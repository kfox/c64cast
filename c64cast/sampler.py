"""Ultimate Audio FPGA PCM sampler ($DF20-$DFFF) — register helpers + a
streaming REU ring that plays arbitrary-length PCM at full fidelity.

The U64 firmware exposes a 7-channel FPGA PCM sampler ("Ultimate Audio",
Gideon's register API v0.2, doc in 1541ultimate/doc/ultimate_audio_v0.2.pdf).
It plays 8- or 16-bit PCM up to 48 kHz **directly out of REU SDRAM** with zero
SID / ``$D418`` / NMI / CPU / turbo involvement — so it is immune to every
bus-halt / badline problem the 4-bit ``$D418`` NMI DAC fights, and is vastly
higher fidelity. On the U64 it is the default video-audio backend; the DAC
stays for TeensyROM (no sampler) and as an opt-in lo-fi mode.

Two halves:

* **Pure register helpers** (unit-testable, no hardware): the channel register
  map, the rate divider, the control byte, the 8/16-bit PCM pack, and a
  byte-layout builder for one channel's registers.
* **``UltimateAudioSampler``** — the scene-facing audio object that mirrors the
  subset of ``audio.AudioStreamer`` that scenes call (``sample_rate``,
  ``position_seconds``, ``stop``, ``push_samples``, ``get_recent_samples``). It
  runs a **streaming REU ring** built on the sampler's own A↔B repeat loop:
  program channel 0 to loop a region of REU forever while gated, then a writer
  thread REUWRITEs decoded PCM *ahead of the computed read head*, wrapping. The
  FPGA's sample clock is crystal-exact, so the read head is **computed** from
  wall-clock (``(monotonic - gate_time) * rate``), never read back — the loop is
  open-loop and drift-free, no servo/governor/NMI needed (much simpler than the
  ``$D418`` REU pump).
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from .backend import C64Backend
    from .dsp import AudioDSP

log = logging.getLogger("c64cast.sampler")

# --------------------------------------------------------------------------
# Register spec (Ultimate Audio v0.2). Multi-byte fields are BIG-ENDIAN.
# --------------------------------------------------------------------------
SAMPLER_IO_BASE = 0xDF20  # channel 0 base; reads here give the IRQ status reg
SAMPLER_VERSION_REG = 0xDF21  # reads $10 when the sampler is present
SAMPLER_CHANNEL_STRIDE = 0x20  # each channel occupies 32 consecutive bytes
SAMPLER_NUM_CHANNELS = 7
SAMPLER_REF_CLOCK = 6_250_000  # the rate divider is REF / sample_rate

# Channel register offsets (relative to the channel base).
REG_CONTROL = 0x00
REG_VOLUME = 0x01  # 0..63
REG_PAN = 0x02  # 7/8 = center, 0 = full left, 15 = full right
REG_START = 0x04  # 4 bytes BE: $01000000 + REU offset
REG_LENGTH = 0x09  # 3 bytes BE: length in bytes (16-bit ⇒ even)
REG_RATE = 0x0E  # 2 bytes BE: divider = round(REF / rate)
REG_REPEAT_A = 0x11  # 3 bytes BE: loop revert point (byte offset in sample)
REG_REPEAT_B = 0x15  # 3 bytes BE: loop end point (byte offset in sample)
REG_INT_CLEAR = 0x1F  # write 1 = clear this channel's IRQ, $FF = all

# Control register bits.
CTRL_GATE = 0x01  # 0→1 (re)starts playback from the sample start
CTRL_REPEAT = 0x02  # loop A↔B while gated; on gate-off, play to end then stop
CTRL_INTERRUPT = 0x04  # raise IRQ at end of sample
CTRL_MODE_8BIT = 0x00  # mode b4-5 = 00
CTRL_MODE_16BIT = 0x10  # mode b4-5 = 01 (little-endian)
CTRL_INTERLEAVE = 0x40  # skip odd samples (stereo-in-REU; unused here)

# The sample start address selects REU SDRAM via the upper address byte $01;
# the lower 24 bits are the REU offset. (The REU base in U2 SDRAM is $01000000.)
REU_ADDR_SELECT_BYTE = 0x01

SAMPLER_VOLUME_MAX = 63
SAMPLER_PAN_CENTER = 7

# --------------------------------------------------------------------------
# Streaming-ring defaults.
# --------------------------------------------------------------------------
# REU offset of the ring. Sits above the $D418-DAC mic ring ($110000) and well
# below the REU-staged video region ($E00000), so the sampler ring coexists
# with REU-staged bitmap video.
DEFAULT_RING_BASE = 0x200000

# Ring size. The ring is a jitter buffer, NOT the playback latency — latency is
# set by the lead/prebuffer target below, independent of ring size. 1 MiB is
# ~5.9 s of headroom at 16-bit/44.1k while keeping the one-time NEUTRAL prefill
# (~1.3 s at REUWRITE's ~820 KB/s) short. The whole track streams through it.
DEFAULT_RING_SIZE = 0x100000  # 1 MiB

# Buffering depth: how far the writer keeps the write head ahead of the read
# head at runtime. This is *not* A/V latency — the video frame is selected by
# the read head (position_seconds), so growing the lead only deepens the
# decode-stall cushion; it doesn't shift sync. Bigger = rides out longer PyAV
# decode hiccups (a 4K clip's per-frame decode briefly starves the single
# demux+push thread — HW-measured lead dipping to ~9 KB / below panic at 0.5 s).
# 1.0 s keeps the lead comfortably above the panic watermark even under 4K
# decode, at the cost only of REU buffered ahead (well under the ring's ~5.9 s).
DEFAULT_LEAD_SECONDS = 1.0

# Startup seed: how much real PCM to prebuffer before gating the channel on.
# Decoupled from (and smaller than) the runtime lead so playback starts
# promptly — the writer then ramps the lead up to DEFAULT_LEAD_SECONDS as the
# demuxer (which races ahead of real time at startup) delivers. The read head
# begins at the first prebuffered sample, so this adds no startup delay.
DEFAULT_PREBUFFER_SECONDS = 0.5

REU_WRITE_SLICE = 32 * 1024  # cap per REUWRITE so a NEUTRAL pad can't burst huge
SAMPLE_TAP_SIZE = 2048  # most-recent-samples tap for spectrum overlays
_INT16_FULL_SCALE = 32768.0


# --------------------------------------------------------------------------
# Pure helpers (no hardware) — directly unit-testable.
# --------------------------------------------------------------------------
def divider_for_rate(rate: float) -> int:
    """Sample-rate divider for the FPGA's 6.25 MHz reference (≥ 1)."""
    if rate <= 0:
        raise ValueError(f"rate must be positive, got {rate}")
    return max(1, round(SAMPLER_REF_CLOCK / rate))


def actual_rate_for_divider(divider: int) -> float:
    """The exact rate the FPGA plays at for a given divider (REF / divider).

    Differs from the nominal request by < 0.5% (e.g. 44100 → div 142 →
    44014.08 Hz). Feeding samples *at this rate* keeps A/V drift-free; the small
    nominal offset is an inaudible constant pitch shift, not a drift."""
    if divider <= 0:
        raise ValueError(f"divider must be positive, got {divider}")
    return SAMPLER_REF_CLOCK / divider


def bytes_per_sample(bits: int) -> int:
    if bits == 8:
        return 1
    if bits == 16:
        return 2
    raise ValueError(f"sampler bits must be 8 or 16, got {bits}")


def control_byte(
    *,
    gate: bool,
    repeat: bool = False,
    interrupt: bool = False,
    bits: int = 16,
    interleave: bool = False,
) -> int:
    """Assemble the control-register byte from its bit fields."""
    value = 0
    if gate:
        value |= CTRL_GATE
    if repeat:
        value |= CTRL_REPEAT
    if interrupt:
        value |= CTRL_INTERRUPT
    value |= CTRL_MODE_16BIT if bits == 16 else CTRL_MODE_8BIT
    if interleave:
        value |= CTRL_INTERLEAVE
    return value


def pack_pcm(samples_int16: np.ndarray, bits: int) -> bytes:
    """Pack mono int16 samples to the sampler's PCM byte format.

    8-bit is **signed** two's-complement (HW-confirmed); 16-bit is signed
    little-endian. The int16→int8 step rounds (not truncates) for fidelity."""
    arr = np.asarray(samples_int16)
    if bits == 8:
        scaled = np.clip(np.rint(arr.astype(np.float32) / 256.0), -128, 127)
        return bytes(scaled.astype(np.int8).tobytes())
    if bits == 16:
        return bytes(np.ascontiguousarray(arr.astype("<i2")).tobytes())
    raise ValueError(f"sampler bits must be 8 or 16, got {bits}")


def channel_base(channel: int) -> int:
    """I/O base address of a sampler channel (0..6)."""
    if not 0 <= channel < SAMPLER_NUM_CHANNELS:
        raise ValueError(f"channel must be 0..{SAMPLER_NUM_CHANNELS - 1}, got {channel}")
    return SAMPLER_IO_BASE + channel * SAMPLER_CHANNEL_STRIDE


def _be_bytes(value: int, nbytes: int) -> list[int]:
    """Big-endian byte list (high byte first), masked to ``nbytes``."""
    return [(value >> (8 * (nbytes - 1 - i))) & 0xFF for i in range(nbytes)]


def channel_register_writes(
    *,
    reu_offset: int,
    length: int,
    divider: int,
    volume: int,
    pan: int,
    repeat: bool,
    repeat_a: int,
    repeat_b: int,
) -> list[tuple[int, list[int]]]:
    """Ordered ``(channel-relative offset, [byte values])`` register writes to
    program a channel, **excluding** the final control/gate write (issue that
    last so playback starts only once every other register is set).

    Pure: builds the exact big-endian byte layout, no hardware. The unit test
    pins this layout."""
    start_addr = (REU_ADDR_SELECT_BYTE << 24) | (reu_offset & 0xFFFFFF)
    writes: list[tuple[int, list[int]]] = [
        (REG_START, _be_bytes(start_addr, 4)),
        (REG_LENGTH, _be_bytes(length, 3)),
        (REG_RATE, _be_bytes(divider, 2)),
        (REG_VOLUME, [volume & 0x3F]),
        (REG_PAN, [pan & 0x0F]),
    ]
    if repeat:
        writes.append((REG_REPEAT_A, _be_bytes(repeat_a, 3)))
        writes.append((REG_REPEAT_B, _be_bytes(repeat_b, 3)))
    return writes


def program_channel(
    api: C64Backend,
    channel: int,
    *,
    reu_offset: int,
    length: int,
    rate: float,
    bits: int,
    volume: int = SAMPLER_VOLUME_MAX,
    pan: int = SAMPLER_PAN_CENTER,
    repeat: bool = False,
    repeat_a: int = 0,
    repeat_b: int = 0,
    gate: bool = True,
) -> None:
    """Program a sampler channel and (optionally) gate it on.

    All non-control registers are written and flushed first, then the control
    byte — so the FPGA never starts playback against a half-written channel."""
    divider = divider_for_rate(rate)
    base = channel_base(channel)
    for offset, values in channel_register_writes(
        reu_offset=reu_offset,
        length=length,
        divider=divider,
        volume=volume,
        pan=pan,
        repeat=repeat,
        repeat_a=repeat_a,
        repeat_b=repeat_b,
    ):
        api.write_regs(f"{base + offset:04X}", *values)
    api.flush()
    if gate:
        ctrl = control_byte(gate=True, repeat=repeat, bits=bits)
        api.write_memory(f"{base:04X}", f"{ctrl:02X}")
        api.flush()


def gate_off(api: C64Backend, channel: int = 0) -> None:
    """Clear a channel's control register (gate off → playback stops)."""
    api.write_memory(f"{channel_base(channel):04X}", "00")
    api.flush()


# --------------------------------------------------------------------------
# Streaming sampler.
# --------------------------------------------------------------------------
class UltimateAudioSampler:
    """Plays arbitrary-length PCM through a streaming REU ring on sampler
    channel 0.

    Lifecycle mirrors the scene-facing slice of ``AudioStreamer``:

        sampler = UltimateAudioSampler(api, sample_rate=44100, bits=16)
        sampler.start()                 # prefill + gate the looping ring
        ...  sampler.push_samples(int16) # writer thread streams it into the ring
        sampler.position_seconds()      # wall-clock read head → A/V master clock
        sampler.stop()                  # gate off, join the writer

    The ring is the sampler's A↔B loop over ``[ring_base, ring_base+ring_size)``.
    A writer thread keeps the write head ~``lead`` bytes ahead of the
    computed read head (``read = (monotonic - gate_time) * rate``), wrapping at
    ``ring_size`` and NEUTRAL-padding on producer underrun so the FPGA never
    reads a stale/lapped byte. No servo: the FPGA clock is exact.
    """

    #: Marker so scenes can duck-type the sampler apart from AudioStreamer
    #: (parallel to the streamer's ``use_reu_pump`` attribute) without importing
    #: this module — VideoScene.setup branches on ``getattr(audio, "is_sampler")``.
    is_sampler = True

    def __init__(
        self,
        api: C64Backend,
        *,
        sample_rate: int = 44100,
        bits: int = 16,
        channel: int = 0,
        dsp: AudioDSP | None = None,
        volume: int = SAMPLER_VOLUME_MAX,
        pan: int = SAMPLER_PAN_CENTER,
        ring_base: int = DEFAULT_RING_BASE,
        ring_size: int = DEFAULT_RING_SIZE,
        lead_seconds: float = DEFAULT_LEAD_SECONDS,
        prebuffer_seconds: float = DEFAULT_PREBUFFER_SECONDS,
        queue_max_chunks: int = 256,
    ) -> None:
        self.api = api
        self.bits = bits
        self.channel = channel
        self._dsp = dsp
        self._volume = volume
        self._pan = pan

        self.bps = bytes_per_sample(bits)
        self._divider = divider_for_rate(sample_rate)
        self._actual_rate = actual_rate_for_divider(self._divider)
        # AVFileSource resamples to this; feeding samples at the FPGA's real
        # rate is what makes the wall-clock read head drift-free.
        self.sample_rate = int(round(self._actual_rate))

        self.ring_base = ring_base
        # Frame-align the ring (16-bit length must be even; the A↔B loop wraps
        # exactly at ring_size so it must be a whole number of samples).
        self.ring_size = (ring_size // self.bps) * self.bps
        self._neutral_unit = b"\x00" * self.bps  # signed PCM silence is zero

        lead_bytes = int(self._actual_rate * lead_seconds) * self.bps
        # Keep the lead under half the ring so write-ahead can't lap the reader.
        self._lead_target = max(self.bps, min(lead_bytes, self.ring_size // 2))
        # Startup prebuffer: seed only this much before gating (fast start),
        # then let the writer ramp up to _lead_target. Clamp to the lead target
        # so a misconfigured prebuffer can't exceed the runtime depth.
        prebuf_bytes = int(self._actual_rate * prebuffer_seconds) * self.bps
        self._prebuffer_target = max(self.bps, min(prebuf_bytes, self._lead_target))
        # Low watermark: the writer only NEUTRAL-pads (inserts silence) once the
        # lead drains this low — a genuine producer stall, not a queue that's
        # briefly empty because the writer is topping up toward the target.
        self._lead_panic = max(self.bps, self._lead_target // 4)

        self._q: queue.Queue[bytes] = queue.Queue(maxsize=queue_max_chunks)
        self._writer: threading.Thread | None = None
        self._running = False
        self._stopped = False
        self._eof = False

        self._gate_time = 0.0
        self._written = 0  # absolute bytes written to the ring (monotone)
        self._pushed_samples = 0  # total source samples accepted via push_samples

        # Telemetry for the teardown log / de-risk.
        self._underrun_pads = 0
        self._lead_min = -1
        self._lead_max = -1

        # Most-recent-samples tap for spectrum-style overlays.
        self._tap_buf = np.zeros(SAMPLE_TAP_SIZE, dtype=np.float32)
        self._tap_write = 0
        self._tap_lock = threading.Lock()

    # ---- bring-up ---------------------------------------------------------
    def start(self, prebuffer_timeout: float = 2.0) -> None:
        """Prefill the ring with silence, prebuffer ``_prebuffer_target`` bytes
        of real PCM, then gate the looping channel on.

        Prefilling the whole ring with NEUTRAL guarantees the FPGA never reads
        uninitialized REU even under a startup jitter spike; the prebuffer seeds
        the write-ahead lead so the writer starts already ahead of the reader.
        Only the (smaller) prebuffer target is seeded before gating — the writer
        then ramps the lead up to ``_lead_target`` — so playback starts promptly
        while the runtime lead stays deep enough to ride out decode stalls."""
        self._prefill_neutral()

        prebuf = self._collect_prebuffer(self._prebuffer_target, prebuffer_timeout)
        if prebuf:
            self._write_wrapped(0, prebuf)
        self._written = len(prebuf)

        program_channel(
            self.api,
            self.channel,
            reu_offset=self.ring_base,
            length=self.ring_size,
            rate=self._actual_rate,
            bits=self.bits,
            volume=self._volume,
            pan=self._pan,
            repeat=True,
            repeat_a=0,
            repeat_b=self.ring_size,
            gate=True,
        )
        self._gate_time = time.monotonic()
        self._running = True
        self._writer = threading.Thread(target=self._writer_loop, name="uaudio-writer", daemon=True)
        self._writer.start()
        log.info(
            "sampler: streaming ring up — %d-bit @ %d Hz (div %d, %.2f Hz actual), "
            "ring %d KiB @ $%06X, lead %.2f s",
            self.bits,
            self.sample_rate,
            self._divider,
            self._actual_rate,
            self.ring_size // 1024,
            self.ring_base,
            self._lead_target / self.bps / self._actual_rate,
        )

    def _prefill_neutral(self) -> None:
        block = self._neutral_unit * (REU_WRITE_SLICE // self.bps)
        for off in range(0, self.ring_size, len(block)):
            n = min(len(block), self.ring_size - off)
            self.api.reu_write(self.ring_base + off, block[:n])
        self.api.flush()

    def _collect_prebuffer(self, want_bytes: int, timeout: float) -> bytes:
        """Drain at least ``want_bytes`` of queued PCM, blocking up to
        ``timeout`` total for the producer to deliver it (whatever arrived by
        then is used — the writer fills the rest ahead of the reader, NEUTRAL
        on underrun). Returns **all** collected bytes (never truncated — a
        partial-chunk truncation would drop samples and glitch the stream)."""
        deadline = time.monotonic() + timeout
        chunks: list[bytes] = []
        have = 0
        while have < want_bytes:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                chunk = self._q.get(timeout=remaining)
            except queue.Empty:
                break
            chunks.append(chunk)
            have += len(chunk)
        return b"".join(chunks)

    # ---- streaming --------------------------------------------------------
    def push_samples(self, samples_int16: np.ndarray) -> None:
        """Accept mono int16 from the demuxer; encode + enqueue for the writer.

        Blocks when the queue is full so PyAV naturally throttles to the
        playback rate (same backpressure as the DAC's ``push_samples``)."""
        if self._stopped:
            return
        floats = samples_int16.astype(np.float32) / _INT16_FULL_SCALE
        self._tap_push(floats)
        if self._dsp is not None and self._dsp.active:
            floats = self._dsp.process(floats)
        out_i16 = np.clip(np.rint(floats * 32767.0), -32768, 32767).astype(np.int16)
        self._pushed_samples += int(samples_int16.shape[0])
        self._q.put(pack_pcm(out_i16, self.bits), block=True)

    def mark_eof(self) -> None:
        """Source exhausted — clamp ``position_seconds`` to the pushed total so
        an over-running wall clock can't desync the (now-ended) video."""
        self._eof = True

    def set_pre_emphasis(self, amount: float | None) -> None:
        """No-op: pre-emphasis is a 4-bit-DAC fidelity aid, irrelevant to the
        16-bit sampler path. Present so the sampler satisfies the same
        scene-facing contract as AudioStreamer (Scene.setup calls this on the
        scene's audio object regardless of backend)."""

    def _read_consumed_bytes(self) -> int:
        if not self._running:
            return 0
        elapsed = time.monotonic() - self._gate_time
        return int(elapsed * self._actual_rate) * self.bps

    def _writer_loop(self) -> None:
        while self._running:
            lead = self._written - self._read_consumed_bytes()
            self._lead_min = lead if self._lead_min < 0 else min(self._lead_min, lead)
            self._lead_max = max(self._lead_max, lead)
            if lead >= self._lead_target:
                # Far enough ahead — idle briefly. The bounded queue + blocking
                # push provide producer backpressure, so the lead can't run away.
                time.sleep(0.002)
                continue
            try:
                data = self._q.get(timeout=0.02)
            except queue.Empty:
                # Queue momentarily empty. NEUTRAL-padding is a *last resort* —
                # it inserts silence into the stream, so only do it when the
                # lead has actually drained to the low watermark (a real
                # underrun → without a pad the FPGA would replay stale ring
                # data, the "echo" the $D418 pump fought). While the lead is
                # still safe, just wait for the producer rather than glitch.
                if lead > self._lead_panic:
                    continue
                pad_frames = max(1, (self._lead_target - lead) // self.bps)
                pad_frames = min(pad_frames, REU_WRITE_SLICE // self.bps)
                data = self._neutral_unit * pad_frames
                self._underrun_pads += 1
            self._write_wrapped(self._written % self.ring_size, data)
            self._written += len(data)

    def _write_wrapped(self, ring_pos: int, data: bytes) -> None:
        """REUWRITE ``data`` into the ring at ``ring_pos``, splitting at the ring
        boundary and capping each transfer at one slice."""
        view = memoryview(data)
        pos = ring_pos
        while view:
            room = self.ring_size - pos
            n = min(len(view), room, REU_WRITE_SLICE)
            self.api.reu_write(self.ring_base + pos, bytes(view[:n]))
            view = view[n:]
            pos += n
            if pos >= self.ring_size:
                pos = 0

    # ---- clock ------------------------------------------------------------
    def position_seconds(self) -> float:
        """Wall-clock seconds since the ring was gated on — the heard playback
        position (same contract as ``AudioStreamer.position_seconds`` in REU-pump
        mode). Clamped to the pushed total after EOF. The FPGA crystal vs the
        host monotonic clock differ by ~ppm, so this is drift-free for A/V sync."""
        if not self._running:
            return 0.0
        elapsed = time.monotonic() - self._gate_time
        if self._eof and self._pushed_samples:
            total_s = self._pushed_samples / self._actual_rate
            return max(0.0, min(elapsed, total_s))
        return max(0.0, elapsed)

    # ---- sample tap (spectrum overlays) -----------------------------------
    def _tap_push(self, mono_floats: np.ndarray) -> None:
        n = mono_floats.shape[0]
        with self._tap_lock:
            if n >= SAMPLE_TAP_SIZE:
                self._tap_buf[:] = mono_floats[-SAMPLE_TAP_SIZE:]
                self._tap_write = 0
                return
            end = self._tap_write + n
            if end <= SAMPLE_TAP_SIZE:
                self._tap_buf[self._tap_write : end] = mono_floats
            else:
                split = SAMPLE_TAP_SIZE - self._tap_write
                self._tap_buf[self._tap_write :] = mono_floats[:split]
                self._tap_buf[: end - SAMPLE_TAP_SIZE] = mono_floats[split:]
            self._tap_write = end % SAMPLE_TAP_SIZE

    def get_recent_samples(self, n: int) -> np.ndarray:
        """Most recent ``n`` float samples (oldest first), a fresh copy."""
        n = min(int(n), SAMPLE_TAP_SIZE)
        out = np.empty(n, dtype=np.float32)
        with self._tap_lock:
            w = self._tap_write
            start = (w - n) % SAMPLE_TAP_SIZE
            tail = SAMPLE_TAP_SIZE - start
            if n <= tail:
                out[:] = self._tap_buf[start : start + n]
            else:
                out[:tail] = self._tap_buf[start:]
                out[tail:] = self._tap_buf[: n - tail]
        return out

    # ---- shutdown ---------------------------------------------------------
    def stop(self) -> None:
        """Gate the channel off and join the writer thread. Firmware-config
        restore (Audio Mixer / I/O map) is separate, in doctor at teardown."""
        self._stopped = True
        self._running = False
        if self._writer is not None:
            self._writer.join(timeout=1.0)
            self._writer = None
        try:
            gate_off(self.api, self.channel)
        except Exception as e:  # best-effort; teardown must not raise
            log.debug("sampler gate-off failed: %s", e)
        while not self._q.empty():
            try:
                self._q.get_nowait()
            except queue.Empty:
                break
        if self._underrun_pads:
            log.warning(
                "sampler: %d underrun pads this session (producer stalled)",
                self._underrun_pads,
            )
        if self._lead_min >= 0:
            log.info(
                "sampler: write-ahead lead min=%d max=%d bytes (target=%d, ring=%d)",
                self._lead_min,
                self._lead_max,
                self._lead_target,
                self.ring_size,
            )
