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

import dataclasses
import functools
import logging
import queue
import secrets
import threading
import time
from collections.abc import Callable
from typing import Any

import numpy as np

from c64cast._teardown import run_teardown_steps
from c64cast.hw.backend import C64Backend
from c64cast.hw.c64 import (
    CIA1,
    CIA2,
    KERNAL,
    REU,
    SID,
    VECTORS,
    halt_quantum_bytes,
    kernal_cia1_latch,
)

from .audio_handlers import (
    AUDIO_HEALTH_LOG_INTERVAL_S,
    AUDIO_QUEUE_MAX_BLOBS,
    AUDIO_WRITE_RATE_SHARE,
    BACKPRESSURE_SPIN_S,
    CHUNK_SIZE,
    CIA2_CRA_STOP,
    CIA2_ICR_DISABLE_ALL,
    CIA_TIMER_LATCH_MAX,
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
    REU_AUDIO_MAX_BYTES,
    REU_AUDIO_SRC_TRACKER_ADDR,
    REU_IRQ_HANDLER,
    REU_IRQ_HANDLER_CHUNK_OFFSETS,
    REU_IRQ_HANDLER_GOVERNOR,
    REU_IRQ_HANDLER_GOVERNOR_CHUNK_OFFSETS,
    REU_IRQ_HANDLER_TRACKED,
    REU_IRQ_HANDLER_TRACKED_CHUNK_OFFSETS,
    REU_MIC_BASE,
    REU_MIC_BOOTSTRAP_BYTES,
    REU_MIC_IRQ_HANDLER,
    REU_MIC_SIZE,
    REU_PUMP_BODY_SUBROUTINE,
    REU_PUMP_BODY_SUBROUTINE_ADDR,
    REU_PUMP_CHUNK_SIZE,
    REU_PUMP_HANDLER_ADDR,
    REU_PUMP_INITIAL_MARGIN,
    REU_PUMP_SETTLE_S,
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
    WORKER_JOIN_TIMEOUT_S,
    encode_floats_to_dac,
    patch_chunk_size,
    stomp_spans,
)
from .audio_rate import NmiTimer, RateServo
from .dac_curves import NEUTRAL_INDEX, resolve_dac_curve
from .dsp import AudioDSP, DSPParams

log = logging.getLogger(__name__)

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


def resolve_audio_input_device(device: int | str) -> int:
    """Map a ``[audio].device`` value (int index, int-in-string, or a device
    *name substring*) to a sounddevice input index.

    Returns ``-1`` ("use the system default input") for a negative/empty value,
    when sounddevice is unavailable, or when a name matches nothing. Unlike the
    camera resolver (:func:`c64cast.control.camera.resolve_camera_index`) this never
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
    """
    return indata.mean(axis=1) if indata.ndim > 1 else indata


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
        # Uploads the skip-when-ahead governor handler so the pump self-throttles
        # with zero host bus writes; False uploads the open-loop handler, which
        # drifts into an echo. Plain (non-bank-swap) path only — the tracked
        # video path ignores this flag.
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
        # Each item is a pre-encoded bytes blob (one byte per sample), so the
        # queue costs one lock per chunk rather than per sample. q.qsize()
        # therefore counts blobs: backpressure reads self._queued_samples, and
        # q.full() is unused because the cap below is in bytes, not items.
        self.q: queue.Queue[bytes] = queue.Queue(maxsize=AUDIO_QUEUE_MAX_BLOBS)
        self._queued_samples = 0
        # flush() and stop() bump _flush_epoch; _encode_and_enqueue and _worker
        # each capture it and discard audio held across a change, so neither a
        # seek/loop/pause splice nor a scene cut-over can leak pre-splice samples
        # from a blocked pusher or the worker's hand. _count_lock pairs the _pushed_count/_queued_samples
        # mutations so position_seconds() (= pushed - queued) stays invariant
        # across a flush drain. _stomp_requested asks the worker (which owns
        # write_addr) to NEUTRAL-fill the unplayed ring, keeping ring DMA off the
        # playlist thread and away from the servo.
        self._flush_epoch = 0
        self._count_lock = threading.Lock()
        self._stomp_requested = False
        # MAX_QUEUED_SAMPLES caps the buffer so a stalled consumer cannot
        # accumulate a wall of stale audio.
        self._max_queued_samples = MAX_QUEUED_SAMPLES
        self.running = False
        # Bumped by every _start_worker; a worker exits when it stops matching.
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
        self.analysis_sink: Callable[[np.ndarray], None] | None = None
        self._analysis_sink_failed = False

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
            # Past the deadline, still take what is already waiting rather than
            # reporting an underrun over a full queue: one over-long sub-write
            # expires every later drip slot, and without this drain that
            # starves collection and NEUTRAL-pads every chunk.
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
                self._late_worst_window_s = max(self._late_worst_window_s, -sleep_s)
            piece = payload[i * quantum : (i + 1) * quantum]
            if not piece:
                break
            self.api.write_memory_file(f"{addr:04X}", piece)
            addr += len(piece)
            if addr >= RING_BUFFER_END:
                addr = RING_BUFFER_ADDR
        return n, taken_total, leftover

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
        self._worker_generation += 1
        thread = threading.Thread(
            target=self._worker,
            args=(self._worker_generation,),
            daemon=True,
            name="audio-worker",
        )
        thread.start()
        return thread

    def _consume_queued(self, n: int) -> None:
        """Account for ``n`` queued bytes that have now LANDED in the ring.

        Only the queued count drops, so ``position = pushed - queued`` advances
        by exactly ``n`` — the audio clock moves because the audio played."""
        if not n:
            return
        with self._count_lock:
            self._queued_samples = max(0, self._queued_samples - n)

    def _discard_unpushed(self, n: int) -> None:
        """Account for ``n`` bytes dropped before they reached the ring.

        Both counts drop — the paired subtract — so the bytes read as never
        pushed and ``position = pushed - queued`` is exactly unchanged across
        the drop. That invariant is what lets flush() splice the transport
        without moving the audio clock, and the only difference from
        :meth:`_consume_queued` is whether the bytes were played: getting the
        two the wrong way round shifts A/V sync at every splice. Both live
        here, once, rather than hand-written at each site."""
        if not n:
            return
        with self._count_lock:
            self._queued_samples = max(0, self._queued_samples - n)
            self._pushed_count = max(0, self._pushed_count - n)

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
        never snapped forward on an overrun. With `host_dma_servo` on
        (default) the increment is `servo.next_pace_increment(...)` instead of
        the bare `chunk_period`, still added to the absolute time, and clamped
        to [0.5, 1.5]·chunk_period so one bad reading cannot stall or sprint
        the schedule.

        See docs/architecture/audio.md#the-worker-thread-and-its-pacing."""
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
            # Pace + collect deadlines. Zero until NMI starts.
            next_write_time = 0.0
            # Last iteration's chunk, dripped out over this one: one chunk_period
            # of latency for collect/write overlap. The queued-sample count drops
            # only once the bytes land, so position_seconds() stays true.
            pending: bytes | None = None
            pending_addr = RING_BUFFER_ADDR
            pending_from_queue = 0
            pending_epoch = 0

            while self.running and generation == self._worker_generation:
                # Captured before the collect: if flush() bumps it while this
                # iteration holds data, that data is pre-splice and is dropped
                # before the ring write below.
                epoch = self._flush_epoch
                pace_deadline = next_write_time if prebuffered else 0.0

                n = 0
                from_queue = 0

                if prebuffered and pending is not None:
                    if epoch != pending_epoch:
                        # Splice landed after this chunk left the queue: drop it
                        # unplayed, with the paired subtract used below.
                        self._discard_unpushed(pending_from_queue)
                        # write_addr passed this chunk at hand-off and
                        # pending_addr is already beyond it, so nothing would
                        # ever write [pending_addr, +len) and the NMI would
                        # replay it from a lap ago as an echo where the splice
                        # promises silence. Filling it also keeps w_head honest,
                        # so the servo isn't handed a W a chunk behind the head.
                        self._neutral_fill_ring(pending_addr, len(pending))
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
                            self._stomp_requested = False
                            self._stomp_ring(pending_addr)
                        n, from_queue, leftover = self._drip_chunk(
                            pending, pending_addr, chunk_buf, leftover, pace_deadline, chunk_period
                        )
                        self._consume_queued(pending_from_queue)
                        w_head = pending_addr + len(pending)
                        if w_head >= RING_BUFFER_END:
                            w_head -= RING_BUFFER_SIZE
                        pending = None
                        pending_from_queue = 0

                if pending is None and n < self.chunk_size:
                    # Priming, or the drip's interleaved slots did not fill the
                    # chunk: fall back to a blocking collect on the same deadline.
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
                    if prebuffered:
                        # Consumption-phase only: with no NMI reading yet, a short
                        # prebuffer collect is a slow start, not an underrun.
                        self._partial_underruns += 1

                # A splice landed while this chunk was in hand: from_queue +
                # leftover are pre-splice, so count them as never pushed (the
                # paired subtract holds position) and skip the write and pace.
                if self._flush_epoch != epoch:
                    self._discard_unpushed(from_queue + len(leftover))
                    leftover = b""
                    continue

                # Pause fast mute, priming iteration only; steady state goes
                # through the pending path above, which stomps against the chunk
                # about to go out rather than this one.
                if self._stomp_requested and prebuffered:
                    self._stomp_requested = False
                    self._stomp_ring(write_addr)

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
                    pending_epoch = epoch
                    write_addr += n
                    if write_addr >= RING_BUFFER_END:
                        write_addr = RING_BUFFER_ADDR
                    next_write_time += self.servo.next_pace_increment(w_head, chunk_period)
                    self._maybe_log_health(time.monotonic())
                    continue

                # Prebuffer fill: the NMI is not consuming yet, so there is no
                # halt to hide from and one unsplit write primes the ring fastest.
                self.api.write_memory_file(f"{write_addr:04X}", bytes(chunk_buf[:n]))
                self._consume_queued(from_queue)
                write_addr += n
                if write_addr >= RING_BUFFER_END:
                    write_addr = RING_BUFFER_ADDR
                w_head = write_addr

                bytes_prebuffered += n
                if bytes_prebuffered >= prebuffer_bytes:
                    self.nmi.start(adaptive=self.nmi_rate_adaptive)
                    prebuffered = True
                    # R only becomes meaningful now that the NMI consumes: start
                    # the servo integrator and rate loop clean (the warm-up gate
                    # arms inside reset_for_consumer_start).
                    self.servo.reset_for_consumer_start()
                    # Health windows measure the consuming phase only — the
                    # prebuffer fill writes unsplit and has no slots to be late.
                    self._health_last_log = 0.0
                    # Pace one chunk_period out so the PREBUFFER_CHUNKS slack
                    # holds instead of being eaten immediately.
                    next_write_time = time.monotonic() + chunk_period
        except Exception:
            # Clearing `running` is what stats()["running"] reports, so a caller
            # can tell a dead worker from a live one rather than inferring it
            # from silence.
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

    def _push_to_analysis(self, mono_floats: np.ndarray) -> None:
        """Feed the pre-DSP analysis sink, if one is installed.

        Called from realtime callbacks, so a failing analyzer must never take
        the audio path down with it: the first exception is logged and the sink
        is dropped for the rest of the run (visuals stop reacting, sound keeps
        playing)."""
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
        # Captured at entry: if flush() bumps it while this call is parked in
        # the backpressure spin below, the samples are pre-splice and are
        # dropped just before the put.
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
            deadline = time.monotonic() + QUEUE_PUT_TIMEOUT_S
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
        # Drop the blob if a splice flushed while we encoded or waited for
        # capacity, else it lands in the queue right after the drain. The
        # residual epoch-check→put window is µs against a user-rate flush.
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

    def _mic_callback(self, indata: np.ndarray, frames: int, time_info: Any, status: Any) -> None:
        if status or not self.running:
            return
        mono = downmix_to_mono(indata)
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
        mono = downmix_to_mono(indata)
        mono = mono * self.sensitivity
        self._push_to_analysis(mono.astype(np.float32, copy=False))
        if not self._dsp_active():
            mono[np.abs(mono) < self.noise_gate] = 0
        mono = self._apply_dsp(mono.astype(np.float32, copy=False))
        self._push_to_tap(mono)
        vol = self._encode_dac(mono)
        self._push_mic_to_reu(vol.tobytes())

    def _push_mic_to_reu(self, encoded: bytes) -> None:
        """REUWRITE `encoded` to the mic ring at `_mic_reu_write_pos`,
        wrapping at REU_MIC_SIZE. Splits the write across the ring boundary
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
        pos = self._mic_reu_write_pos
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
        # the REU delegation both see a plain int.
        device = resolve_audio_input_device(device)
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
        self._pushed_count = 0
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

    def _program_reu_pump_rate(self, chunk: int) -> int:
        """Derive the matched CIA #1 Timer A latch for ``chunk`` bytes per pump
        IRQ, record it as this run's nominal, write it to $DC04/$DC05, and
        return it for the caller's log line.

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
        only the fast end of ``sample_rate``.

        CIA #1 stays in continuous mode (the kernal already set CRA); only the
        latch changes. BASIC's TI$ jiffy clock drifts as a side effect —
        nothing we depend on.
        """
        ideal = chunk * (self.nmi.nominal_latch() + 1) - 1
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
        self.api.write_memory(
            f"{CIA1.TIMER_A_LO:04X}", f"{latch & 0xFF:02X}{(latch >> 8) & 0xFF:02X}"
        )
        return latch

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
        (_program_reu_pump_rate, REU_PUMP_SETTLE_S, the handler constants).
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
        for off in range(0, REU_MIC_SIZE, REU_UPLOAD_SLICE):
            n = min(REU_UPLOAD_SLICE, REU_MIC_SIZE - off)
            self.api.reu_write(REU_MIC_BASE + off, pad[:n])

        # Standard NMI bring-up (handler + ring + digi-boost). NMI consumes from
        # the $4000 ring _upload_nmi_and_buffers just NEUTRAL-filled.
        self._upload_nmi_and_buffers()

        # Install the mic IRQ handler at $C100 and seed the main-RAM REU source
        # tracker at $C200 with REU_MIC_BASE; the handler reloads
        # $DF04/$DF05/$DF06 from it every IRQ, around the $DF06 read-back
        # garbage documented in audio_handlers.py. REU regs: dest =
        # RING_BUFFER_ADDR, length = REU_PUMP_CHUNK_SIZE, address-control = 0
        # (both auto-inc, no autoload). src needs no init — the handler writes
        # it on every trigger.
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

        # Match the pump rate to the NMI consume rate, derived from the live NMI
        # latch by the same helper the video path uses (_program_reu_pump_rate
        # says why it cannot be a constant).
        cia1_latch = self._program_reu_pump_rate(REU_PUMP_CHUNK_SIZE)
        self.api.flush()
        log.info(
            "audio[reu mic]: pump installed at $%04X, CIA #1 latch=$%04X",
            REU_PUMP_HANDLER_ADDR,
            cia1_latch,
        )

        # Arm NMI (CIA #2 Timer A). NMI now consumes the prebuilt
        # NEUTRAL ring at the consume rate.
        self._reu_pump_start_time = time.monotonic()
        self.nmi.start(adaptive=self.nmi_rate_adaptive)
        time.sleep(REU_PUMP_SETTLE_S)  # let NMI catch a few samples first

        # Patch the IRQ vector at the mic pump handler; it starts on the next
        # kernal IRQ (~16 ms), reading NEUTRAL until the bootstrap window has
        # passed. Skipped when the display mode's bank-swap dispatcher owns
        # $0314 and JMPs to $C100 itself.
        if not skip_irq_vector_hook:
            self.api.write_regs(
                f"{VECTORS.IRQ:04X}",
                REU_PUMP_HANDLER_ADDR & 0xFF,
                (REU_PUMP_HANDLER_ADDR >> 8) & 0xFF,
            )
            self.api.flush()

        self.running = True
        self._reu_pump_armed = True
        self._pushed_count = 0
        # Start the host write head ahead of the pump's read head: steady-state
        # latency is REU_MIC_BOOTSTRAP_BYTES / sample_rate, ~133 ms at 12 kHz.
        self._mic_reu_write_pos = REU_MIC_BOOTSTRAP_BYTES

        # _open_input_stream hardcodes self._mic_callback, so swap in the REU
        # variant for this path.
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
            # The "falling back" warning below already says what happened.
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

    def start_for_external_source(self) -> None:
        """Bring up NMI + worker without an input thread. Caller feeds samples
        via push_samples()."""
        self._listen_mode = False
        self._upload_nmi_and_buffers()
        self._pushed_count = 0
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
        where the C64 bus is heavily halted (e.g. mhires DMAWRITE). The pump
        production rate is chunk × pump_irq_rate; when NMI consumption drops
        below the configured sample_rate due to bus halts, a smaller chunk keeps
        the ring from overflowing. See REU_PUMP_CHUNK_SIZE_HEAVY_BUS for the measured
        value (4020 Hz NMI under mhires-like halts → ~65 bytes/IRQ).

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
        if not audio_4bit:
            log.warning("audio: start_for_reu_staged called with empty data")
            return
        self._listen_mode = False
        chunk = REU_PUMP_CHUNK_SIZE if chunk_size is None else chunk_size
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
        for off in range(0, len(audio_4bit), REU_UPLOAD_SLICE):
            self.api.reu_write(REU_AUDIO_BASE + off, audio_4bit[off : off + REU_UPLOAD_SLICE])
            if on_progress is not None:
                on_progress(min(off + REU_UPLOAD_SLICE, len(audio_4bit)) / upload_total)
        # EOF pad: write NEUTRAL_SAMPLE for the tail so the pump's read-past-
        # end-of-source plays silence instead of garbage.
        pad_payload = bytes([self._neutral_byte] * REU_UPLOAD_SLICE)
        pad_off = len(audio_4bit)
        pad_end = pad_off + eof_pad_bytes
        while pad_off < pad_end:
            chunk_len = min(REU_UPLOAD_SLICE, pad_end - pad_off)
            self.api.reu_write(REU_AUDIO_BASE + pad_off, pad_payload[:chunk_len])
            pad_off += chunk_len
            if on_progress is not None:
                on_progress(pad_off / upload_total)
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
        # $C200-$C204 (src LO/MI/HI, dst LO/HI) every IRQ.
        #
        # Chunk-operand offsets come from audio_handlers' *_CHUNK_OFFSETS,
        # stated beside the assembly that defines them: a wrong offset writes a
        # length into another instruction's operand and DMAs to a garbage
        # address.
        if skip_irq_vector_hook:
            handler = patch_chunk_size(
                REU_IRQ_HANDLER_TRACKED, REU_IRQ_HANDLER_TRACKED_CHUNK_OFFSETS, chunk
            )
            # Seed src + dst trackers BEFORE uploading the tracked handler: in
            # between, a CIA #1 IRQ through the bank-swap dispatcher would run
            # the handler on stale trackers and DMA to garbage addresses (static
            # into the ring, writes into color RAM). The bank-swap install's JMP
            # $EA31 stub at $C100 covers the window, and the handler upload then
            # swaps it out atomically once the tracker is valid.
            self.api.write_memory(
                f"{REU_AUDIO_SRC_TRACKER_ADDR:04X}",
                f"{initial_src_off & 0xFF:02X}"
                f"{(initial_src_off >> 8) & 0xFF:02X}"
                f"{(initial_src_off >> 16) & 0xFF:02X}"
                f"{initial_dst & 0xFF:02X}"
                f"{(initial_dst >> 8) & 0xFF:02X}",
            )
            # Seed the tick divider to 1 so the first IRQ DECs to 0, reloads N
            # and chains. Unseeded, $C205 holds whatever was in RAM — 0 wraps to
            # $FF on the DEC, costing 254 lean exits (~2.5 s of unresponsive
            # keyboard) before the first kernal tail.
            self.api.write_memory(f"{REU_PUMP_TICK_COUNTER_ADDR:04X}", "01")
            # Upload the pump body at $C180 BEFORE the entry at $C100: the
            # chunked mhires dispatcher JSRs to $C180 between per-frame REC
            # chunks, so an entry installed first lets a mid-install CIA #1 IRQ
            # call into uninitialized RAM.
            self.api.write_memory_file(
                f"{REU_PUMP_BODY_SUBROUTINE_ADDR:04X}", REU_PUMP_BODY_SUBROUTINE
            )
            self.api.write_memory_file(f"{REU_PUMP_HANDLER_ADDR:04X}", handler)
        elif self.reu_pump_governor:
            # Governor handler: skip-when-ahead prefix + the pump body.
            handler = patch_chunk_size(
                REU_IRQ_HANDLER_GOVERNOR, REU_IRQ_HANDLER_GOVERNOR_CHUNK_OFFSETS, chunk
            )
            self.api.write_memory_file(f"{REU_PUMP_HANDLER_ADDR:04X}", handler)
        else:
            handler = patch_chunk_size(REU_IRQ_HANDLER, REU_IRQ_HANDLER_CHUNK_OFFSETS, chunk)
            self.api.write_memory_file(f"{REU_PUMP_HANDLER_ADDR:04X}", handler)
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

        # Reprogram CIA #1 Timer A latch for the matched pump rate (see
        # _program_reu_pump_rate, which the mic bring-up shares).
        cia1_latch = self._program_reu_pump_rate(chunk)

        self.api.flush()
        log.info(
            "audio: REU pump installed at $%04X, chunk=%d, CIA #1 latch=$%04X",
            REU_PUMP_HANDLER_ADDR,
            chunk,
            cia1_latch,
        )

        # Arm the NMI on the pre-filled ring, capturing the playback-clock
        # origin immediately before it starts firing: position_seconds() must
        # measure time since audio became audible, or video sync trails it by
        # the bring-up cost.
        self._reu_pump_start_time = time.monotonic()
        self.nmi.start(adaptive=self.nmi_rate_adaptive)

        # Brief settle so NMI is already firing before the REU pump arms
        # (see REU_PUMP_SETTLE_S).
        time.sleep(REU_PUMP_SETTLE_S)

        # Patch the IRQ vector at the pump handler; it starts on the next kernal
        # IRQ (~16 ms). Skipped when the display mode's bank-swap dispatcher owns
        # $0314 and JMPs to $C100 itself.
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
        run_teardown_steps(
            log,
            type(self).__name__,
            [
                # write_regs coalesces into one DMA, so $0314 and $0315
                # atomically point at the kernal.
                (
                    "IRQ vector restore",
                    lambda: self.api.write_regs(
                        f"{VECTORS.IRQ:04X}",
                        KERNAL.IRQ_HANDLER & 0xFF,
                        (KERNAL.IRQ_HANDLER >> 8) & 0xFF,
                    ),
                ),
                ("CIA #1 Timer A latch restore", self._restore_cia1_latch),
                ("REU pump disarm flush", self.api.flush),
            ],
        )
        self._reu_pump_armed = False

    def _restore_cia1_latch(self) -> None:
        """Put CIA #1 Timer A back to this machine's kernal default, without
        which the jiffy clock, `SCNKEY` and the cursor blink stay at the REU
        pump's rate. Raises on a `system` that resolves to neither NTSC nor
        PAL."""
        latch = kernal_cia1_latch(self.system)
        self.api.write_memory(
            f"{CIA1.TIMER_A_LO:04X}", f"{latch & 0xFF:02X}{(latch >> 8) & 0xFF:02X}"
        )

    def push_samples(self, samples_int16: np.ndarray) -> None:
        """Convert mono int16 → 4-bit volume codes and enqueue. Blocks
        briefly when the queue is full so the PyAV demuxer naturally
        throttles to the audio sample rate. A no-op once stopped, as the
        sampler's is."""
        if not self.running:
            return
        floats = samples_int16.astype(np.float32) / INT16_FULL_SCALE
        # Pre-DSP analysis tap, as in the mic callbacks, so a decoded file
        # drives reactive visuals through the same analyzer.
        self._push_to_analysis(floats)
        self._encode_and_enqueue(floats, block_on_full=True)

    def position_seconds(self) -> float:
        """Approximate playback position from the consumer's perspective.

        Host-DMA mode: (samples pushed - samples still queued) / effective_rate.
        REU pump mode: wall-clock seconds since the IRQ pump armed, clamped to
        the total source length so over-runs don't desync video — but only when
        there IS a total. A live REU-mic session has no finite length and never
        sets one, and clamping a wall clock to a zero total pinned it at 0.0 for
        the whole session (or, on a streamer reused after a staged video scene,
        to the previous track's length). The C64 ring buffer adds another ~0.5s
        of latency past either path, but that bias is constant in steady state
        and therefore harmless for relative sync.

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
        bare queue drain would leave open. Every drop goes through
        :meth:`_discard_unpushed`, whose paired subtract leaves
        ``position = pushed - queued`` unchanged.
        No-op in REU-pump mode (no host queue to flush)."""
        if self._reu_pump_armed:
            return
        self._flush_epoch += 1
        self._discard_unpushed(self._drain_queue_samples())
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
        self.running = False
        # Ahead of everything a producer could outlast: the push path's epoch
        # check is what drops a blob from a producer this clear just released,
        # and the drain at the bottom only catches one that beats it there.
        self._flush_epoch += 1
        # No-op if the pump was never armed. The governor lives entirely in the
        # C64-side handler, so disarming the IRQ vector stops it.
        self._disarm_reu_pump()
        run_teardown_steps(log, type(self).__name__, self._hardware_teardown_steps())
        # NMI is already silenced; let the worker / mic threads tear down
        # at their own pace.
        self._close_mic_stream()
        if self._worker_thread:
            # A plain bounded join, not session.join_bounded: a daemon thread
            # joined off the main thread, and the audio layer must not import
            # the app layer.
            self._worker_thread.join(timeout=WORKER_JOIN_TIMEOUT_S)
            if self._worker_thread.is_alive():
                # A ring write on a stalled link can outlast the bounded join,
                # and the counters cleared just below are still being mutated.
                # Dropping the reference is safe: the surviving worker is
                # generation-fenced (see _worker), so the next start_* cannot
                # resurrect it into a second live writer.
                log.warning(
                    "audio: worker did not exit within %.1fs; ring writes may still "
                    "be in flight (it will exit when its write returns)",
                    WORKER_JOIN_TIMEOUT_S,
                )
            self._worker_thread = None
        # Drain so subsequent runs start clean. The epoch bumped above is what
        # covers a producer still between its own capture and its put.
        self._drain_queue_samples()
        self._pushed_count = 0
        self._queued_samples = 0
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
        # Clear the timer's pitch-comp/arm state and the servo's watchdog +
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
