"""Ultimate Audio FPGA PCM sampler ($DF20-$DFFF) — register helpers + a
streaming REU ring that plays arbitrary-length PCM at full fidelity.

The U64 firmware exposes a 7-channel FPGA PCM sampler ("Ultimate Audio",
Gideon's register API v0.2, doc in 1541ultimate/doc/ultimate_audio_v0.2.pdf).
It plays 8- or 16-bit PCM up to 48 kHz **directly out of REU SDRAM** with zero
SID / ``$D418`` / NMI / CPU / turbo involvement. On the U64 it is the default
video-audio backend; the DAC stays for TeensyROM and as an opt-in lo-fi mode.

Two halves: pure register helpers (the channel register map, the rate divider,
the control byte, the 8/16-bit PCM pack, a byte-layout builder), and
``UltimateAudioSampler`` — the scene-facing audio object mirroring the subset
of ``audio.AudioStreamer`` that scenes call, driving a streaming REU ring over
the sampler's own A↔B repeat loop with a wall-clock-computed (never read back)
read head.

See docs/architecture/audio.md#samplerpy--ultimateaudiosampler-u64-ultimate-audio-fpga-pcm.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

import numpy as np

from c64cast._pollthread import PollThread
from c64cast.hw.backend import ULTIMATE_PROFILE, HardwareProfile
from c64cast.hw.c64 import ULTIMATE_AUDIO
from c64cast.hw.delivery import CONFIRM_TRIES, write_confirmed

from .splice import FlushCut

if TYPE_CHECKING:
    from c64cast.hw.backend import C64Backend

    from .dsp import AudioDSP

log = logging.getLogger("c64cast.audio.sampler")

# Register spec (Ultimate Audio v0.2). Multi-byte fields are BIG-ENDIAN.
SAMPLER_IO_BASE = ULTIMATE_AUDIO.IO_BASE  # channel 0; reads give the IRQ status reg
SAMPLER_CHANNEL_STRIDE = 0x20  # each channel occupies 32 consecutive bytes
SAMPLER_NUM_CHANNELS = 7
# DESIGN value from the firmware's sampler2.vhd (50 MHz effective / 8 cycles
# per sample tick); the divider table derives from it and tests pin it. The
# real FPGA clock deviates — see SAMPLER_REF_CLOCK_DEFAULT.
SAMPLER_REF_CLOCK = 6_250_000  # the rate divider is REF / sample_rate

# The shipping U64 firmware clocks the sampler ~1.44% SLOW against the 6.25 MHz
# design nominal. HW-measured 6.157-6.16 MHz with
# scripts/diags/sampler_av_align_calib.py (36 markers / 180 s, differential
# SID-vs-sampler drift); a firmware/FPGA-derivation property, identical across
# units, so it ships as the [audio].sampler_clock_hz default rather than a
# per-unit calibration. Re-measure after any firmware release that changes
# sampler timing — the diag prints the new value. See
# docs/architecture/audio.md#reference-clock-calibration.
SAMPLER_REF_CLOCK_DEFAULT = 6_160_000

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

# Above the $D418-DAC mic ring ($110000) and below REU-staged video ($E00000),
# so the sampler ring coexists with REU-staged bitmap video.
DEFAULT_RING_BASE = 0x200000

# A jitter buffer, NOT the playback latency (that is the lead below). 1 MiB is
# ~11.9 s of mono 16-bit/44.1k, with a one-time NEUTRAL prefill of ~1.3 s at
# REUWRITE's ~820 KB/s.
DEFAULT_RING_SIZE = 0x100000  # 1 MiB

# Runtime write-ahead depth, NOT A/V latency: the video frame tracks the read
# head, so a deeper lead only cushions PyAV decode stalls. HW-measured, a 4K
# clip's lead floor doubled from ~9 KB to ~21 KB going 0.5 s → 1.0 s.
DEFAULT_LEAD_SECONDS = 1.0

# Real PCM seeded before the channel is gated on. Smaller than the runtime
# lead so playback starts promptly; the writer then ramps up to it. The read
# head begins at the first prebuffered sample, so this adds no startup delay.
DEFAULT_PREBUFFER_SECONDS = 0.5
# How often the prebuffer collect re-checks end_input() while the queue is
# empty: the most a short clip's gate waits past its producer's end.
_PREBUFFER_POLL_S = 0.02

REU_WRITE_SLICE = 32 * 1024  # cap per REUWRITE so a NEUTRAL pad can't burst huge
SAMPLE_TAP_SIZE = 2048  # most-recent-samples tap for spectrum overlays
_INT16_FULL_SCALE = 32768.0

# Margin flush() leaves between the computed read head and the first
# NEUTRAL-rewritten byte, covering consumed-estimate jitter, REUWRITE latency,
# and the calibrated-ref residual drift (~1.3 ms / 5 s). It is also the audible
# splice latency; raise it if HW shows a splice click.
FLUSH_GUARD_S = 0.15

# A REU write that raises (a link failure that survived socket_dma's one
# redial) is retried with a doubling back-off between these bounds. The
# channel stops at its deadline once the lead runs out (DEADLINE_GUARD_S);
# after WRITER_GIVE_UP_S of unbroken failure (two of socket_dma's 5 s connect
# timeouts) the writer also gates it off and stops taking audio, so the
# producer is not parked on a queue nothing drains for the rest of the scene.
WRITER_BACKOFF_MIN_S = 0.02
WRITER_BACKOFF_MAX_S = 0.5
WRITER_GIVE_UP_S = 10.0

# Audio is anchored to the read-head clock, so a chunk whose slot has passed
# is dropped. A producer catching up after a stall drops only for a moment; one
# that is late for this long of read-head time without catching up (a decoder
# slower than real time, a live stream resumed at 1.0x, a start whose
# prebuffer timed out) would otherwise stay silent for good. The writer then
# re-anchors the audio a cushion past the read head and plays on, behind the
# picture by the shortfall.
LATE_REANCHOR_S = 0.5

# Catching up means lining up within this long at the pace the lateness
# shrank: a decoder at 1.05x that is 0.85 s late gains 25 ms a window and
# would stay silent for 17 s, so it is re-anchored like one that does not
# gain at all.
LATE_CATCHUP_S = 2.0

# The writer holds a partial write quantum for the rest of it until the lead
# comes within this much of the write floor (FLUSH_GUARD_S past the reader):
# one bounded queue wait (20 ms) plus a REU write.
HOLD_GUARD_S = 0.03

# A partial write quantum goes to the ring no sooner than this after the
# previous audio write, in every state: steady, after a splice, while late, and
# re-anchored. The link carries about 200 writes/s and the picture shares
# them, and below its free-payload knee a write costs the same whatever it
# carries, so the audio's budget is a write count: about 60 a second, of
# which the floor gives partial writes 50, leaving the rest for the whole
# quanta and underrun pads it does not hold (at 1/60 s a 0.99x stream's
# busiest second came to 62). Rules keyed to one state each kept leaving
# another where every 2.5 ms frame went out on its own (~245 writes in the
# second after a splice, ~210 in a first late window, ~400 as a slow stream's
# cushion ran out). A whole quantum is
# not held by it, and a write retried after a failure already waits out the
# writer's back-off (WRITER_BACKOFF_MIN_S, at least this long). It must not
# exceed the writer's bounded queue wait (20 ms): the floor's wait then runs
# inside the one HOLD_GUARD_S already budgets past the hold's deadline, so it
# never holds an on-time gather into lateness. At 25 ms a gather released at
# the deadline was held 10 ms past the write floor and dropped (8 kHz/8-bit,
# fake clock). Audio already at the write floor it does hold late, by up to
# the interval, and the re-anchor rule allows for that (_late_anchor,
# _gaining).
MIN_WRITE_INTERVAL_S = 0.02

# The channel's length register is a dead-man deadline: the FPGA checks it on
# every sample even while looping, and stops there (sampler2.vhd's `finished`
# state). The writer keeps it at the end of what this lap of the ring holds,
# so a link that stops carrying writes stops the sound there, rather than the
# loop replaying the last lap until the link returns: the give-up's gate-off
# travels the dead link too. A deadline the read head gets within this much
# of is taken as reached, and the channel is restarted on the next write that
# lands. It covers how far the FPGA may run ahead of the computed read head;
# the clock is read just before the gate-on goes out, so the gate-on itself
# never puts it ahead.
DEADLINE_GUARD_S = 0.05


def divider_for_rate(rate: float, ref_clock: int = SAMPLER_REF_CLOCK) -> int:
    """Sample-rate divider for the sampler reference clock (≥ 1). ``ref_clock``
    defaults to the nominal 6.25 MHz; pass a per-unit calibrated value to
    compensate a sampler that plays off-speed (see SAMPLER_REF_CLOCK)."""
    if rate <= 0:
        raise ValueError(f"rate must be positive, got {rate}")
    return max(1, round(ref_clock / rate))


def actual_rate_for_divider(divider: int, ref_clock: int = SAMPLER_REF_CLOCK) -> float:
    """The exact rate the FPGA plays at for a given divider (REF / divider).

    Differs from the nominal request by < 0.5% (e.g. 44100 → div 142 →
    44014.08 Hz). Feeding samples *at this rate* keeps A/V drift-free; the small
    nominal offset is an inaudible constant pitch shift, not a drift."""
    if divider <= 0:
        raise ValueError(f"divider must be positive, got {divider}")
    return ref_clock / divider


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


def length_write_intermediates(old: int, new: int) -> set[int]:
    """Every value the 3-byte length register can hold for a moment while one
    write moves it from ``old`` to ``new``, whatever order its bytes land in.

    The FPGA compares the register on every sample while the write is in
    flight, so a value it passes through that equals the play position stops
    the channel there, and nothing on the host would know."""
    old_b, new_b = _be_bytes(old, 3), _be_bytes(new, 3)
    values = set()
    for mask in range(1, 7):  # each byte subset but none and all
        mixed = [new_b[i] if mask >> i & 1 else old_b[i] for i in range(3)]
        values.add(int.from_bytes(bytes(mixed), "big"))
    return values - {old, new}


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
    ref_clock: int = SAMPLER_REF_CLOCK,
) -> None:
    """Program a sampler channel and (optionally) gate it on.

    All non-control registers are written and flushed first, then the control
    byte — so the FPGA never starts playback against a half-written channel.
    ``ref_clock`` must match the one used to derive ``rate`` so the programmed
    divider and the resample target agree (see SAMPLER_REF_CLOCK)."""
    divider = divider_for_rate(rate, ref_clock)
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


class UltimateAudioSampler:
    """Plays arbitrary-length PCM through a streaming REU ring on sampler
    channel 0.

    Lifecycle mirrors the scene-facing slice of ``AudioStreamer``:

        sampler = UltimateAudioSampler(api, sample_rate=44100, bits=16)
        sampler.start()                 # prefill + gate the looping ring
        ...  sampler.push_samples(int16) # writer thread streams it into the ring
        sampler.position_seconds()      # wall-clock read head; less the re-anchor
                                        # lag (audio_source.heard_seconds) → A/V clock
        sampler.stop()                  # gate off, join the writer

    The ring is the sampler's A↔B loop over ``[ring_base, ring_base+ring_size)``.
    A writer thread keeps the write head ~``lead`` bytes ahead of the
    computed read head (``read = (monotonic - gate_time) * rate``), wrapping at
    ``ring_size`` and NEUTRAL-padding on producer underrun so the FPGA never
    reads a stale/lapped byte. No servo: the FPGA clock is exact.
    """

    #: Marker so scenes can duck-type the sampler apart from AudioStreamer
    #: (parallel to the streamer's ``use_reu_pump`` attribute) without importing
    #: this module — AudioFileSource branches on ``getattr(audio, "is_sampler")``
    #: (VideoScene.setup imports the class and uses ``isinstance`` instead).
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
        ref_clock_hz: int = SAMPLER_REF_CLOCK,
    ) -> None:
        self.api = api
        self.bits = bits
        self.channel = channel
        self._dsp = dsp
        self._volume = volume
        self._pan = pan

        # Both the programmed divider and the resample target derive from this,
        # so they stay consistent (see SAMPLER_REF_CLOCK_DEFAULT).
        self._ref_clock = ref_clock_hz
        self.bps = bytes_per_sample(bits)
        self._divider = divider_for_rate(sample_rate, self._ref_clock)
        self._actual_rate = actual_rate_for_divider(self._divider, self._ref_clock)
        # AVFileSource resamples to this; feeding samples at the FPGA's real
        # rate is what makes the wall-clock read head drift-free.
        self.sample_rate = int(round(self._actual_rate))

        self.ring_base = ring_base
        # 16-bit length must be even, and the A↔B loop wraps exactly at
        # ring_size, so the ring is a whole number of samples.
        self.ring_size = (ring_size // self.bps) * self.bps
        self._neutral_unit = b"\x00" * self.bps  # signed PCM silence is zero

        lead_bytes = int(self._actual_rate * lead_seconds) * self.bps
        # Keep the lead under half the ring so write-ahead can't lap the reader.
        self._lead_target = max(self.bps, min(lead_bytes, self.ring_size // 2))
        self._lead_target -= self._lead_target % self.bps
        # Clamped to the lead target so a misconfigured prebuffer can't exceed
        # the runtime depth.
        # What flush() leaves between the read head and the first post-splice
        # byte, and the floor below which a write would race the reader.
        self._flush_margin = int(FLUSH_GUARD_S * self._actual_rate) * self.bps
        prebuf_bytes = int(self._actual_rate * prebuffer_seconds) * self.bps
        self._prebuffer_target = max(self.bps, min(prebuf_bytes, self._lead_target))
        # Low watermark: below this the writer NEUTRAL-pads, treating the lead
        # as a genuine producer stall rather than a briefly-empty queue.
        self._lead_panic = max(self.bps, self._lead_target // 4)
        # Where a re-anchor (LATE_REANCHOR_S) puts the audio: past the write
        # floor, with the low watermark's cushion. A producer slower than real
        # time eats a cushion at its shortfall and then spends another
        # LATE_REANCHOR_S dropping, so re-anchoring at the floor alone left a
        # 0.95x decoder audible ~20% of the time (fake link, 10 ms chunks).
        self._reanchor_lead = max(self._flush_margin, self._lead_panic)
        # The writer moves at least this much per ring write. Below the link's
        # free payload a second write costs a whole per-write floor while the
        # bytes cost nothing, so write count is the lever: a decoder that
        # emits 2.5 ms frames must not become 400 REU writes a second. Half
        # the lead target caps it for a lead too shallow to hold two.
        profile = getattr(api, "profile", None)
        if not isinstance(profile, HardwareProfile):
            profile = ULTIMATE_PROFILE
        quantum = min(profile.free_payload_bytes(), self._lead_target // 2, REU_WRITE_SLICE)
        self._write_quantum = max(self.bps, quantum - quantum % self.bps)
        self._hold_guard = int(HOLD_GUARD_S * self._actual_rate) * self.bps
        self._write_interval = int(MIN_WRITE_INTERVAL_S * self._actual_rate) * self.bps
        # The hold's deadline: a partial gather is carried while the lead is
        # above it (_holds).
        self._hold_deadline = self._flush_margin + self._hold_guard
        # Read head at the latest audio write (_write_payload), for the
        # MIN_WRITE_INTERVAL_S floor; None before the first. Pads and the
        # splice's blank are not counted: they are rare, and a floor timed
        # from the cut-over's blank held the first post-splice audio, whose
        # anchor sits on the write floor, a whole interval into lateness.
        self._last_write_head: int | None = None
        # A real-time producer re-anchored here keeps enough slack over the
        # hold's deadline to gather whole quanta, rather than written frame by
        # frame (a 2.5 ms Opus frame each is 400 writes a second). The second
        # HOLD_GUARD_S is for its frames arriving late: re-anchored one sample
        # past the hold threshold, a frame 6 ms behind its schedule was
        # written on its own (8-bit, 2 kHz, fake clock).
        hold_floor = self._hold_threshold(self.bps) + self._hold_guard + self.bps
        self._reanchor_lead = max(self._reanchor_lead, min(hold_floor, self._lead_target))
        # Writer-owned: the unwritten tail of a chunk larger than one write,
        # tagged with its flush epoch like a queue item.
        self._carry: tuple[int, memoryview] | None = None

        # (flush epoch at push time, packed PCM): the writer drops an item whose
        # epoch is no longer current, so nothing pushed before a splice is
        # written after it.
        self._q: queue.Queue[tuple[int, bytes]] = queue.Queue(maxsize=queue_max_chunks)
        self._writer: PollThread | None = None
        self._running = False
        self._stopped = False
        self._eof = False
        # Set by end_input() once the producer has pushed its last sample for
        # now, so start() stops waiting for a prebuffer a short clip cannot
        # fill. Unlike _eof it leaves the clock alone. Cleared by arm() and by
        # the next accepted push (a video's demuxer pushes again after a seek
        # back), so it is not a "track finished" signal.
        self._input_ended = False
        # Set when the writer gave up on a dead link; push_samples then drops
        # rather than park the producer on a queue nothing drains.
        self._failed = False

        self._gate_time = 0.0
        self._held = False
        # Absolute byte positions (the read head is (monotonic - gate) * rate).
        # _written is how far the ring holds anything current, real or pad;
        # _content_pos is where the next real sample belongs. Sample k of an
        # activation (or of a splice) is anchored at a fixed position, so an
        # underrun pad is provisional: real data overwrites the part not yet
        # played, and data whose slot the reader already passed is dropped.
        # Audio therefore stays on position_seconds()'s wall clock instead of
        # sliding later by every pad.
        self._written = 0
        self._content_pos = 0
        self._late_bytes = 0  # real PCM dropped because its slot had passed
        # The lateness-trend rule (_late_anchor). Lateness is how far the
        # audio's anchor sits behind the write floor, in bytes, measured at
        # each write attempt; the read head is the wall clock, so windows are
        # wall time. _late_ref is (read head, lateness) where the current
        # window began, None while writes land on time. A burst is a run of
        # attempts with no LATE_REANCHOR_S gap between them: _burst_start is
        # (read head, lateness) at the current one's first late attempt,
        # _prev_start the previous one's. _last_try is the read head at the
        # latest write attempt, one dropped whole included; an attempt whose
        # ring write failed does not count (_write_payload puts the one before
        # it back). Whether a gap preceded the first late attempt of an
        # activation or a splice does not matter: with no burst behind it, a
        # gap and a fresh window start the same way. _late_from is the
        # read head at the first late attempt since writes were last on time
        # (or since the last splice, arm() or re-anchor), so the re-anchor's
        # WARNING says how long the audio was late: across gaps that is
        # several bursts, not one window. _last_late is the read head at the
        # latest late attempt: writes on time by less than the interval do
        # not end a window, but a whole LATE_REANCHOR_S of them does, so a
        # window never outlives the lateness that opened it.
        self._late_ref: tuple[int, int] | None = None
        self._burst_start: tuple[int, int] | None = None
        self._prev_start: tuple[int, int] | None = None
        self._last_try: int | None = None
        self._last_late: int | None = None
        self._late_from: int | None = None
        # Set by a re-anchor, cleared by the next splice or arm(): the producer
        # has shown it cannot keep up, so later late audio is re-anchored at
        # once instead of dropped for another window. It then plays late, as
        # it did before audio was anchored, rather than losing a window of
        # every cycle (~20% of a 0.95x decoder).
        self._reanchor_sticky = False
        self._late_reanchor_bytes = int(LATE_REANCHOR_S * self._actual_rate) * self.bps
        self._late_catchup_bytes = int(LATE_CATCHUP_S * self._actual_rate) * self.bps
        # Re-anchors this activation, and how far they put the sound behind
        # the picture since the last splice re-aligned it: (every shift's
        # total, the holds the read head has not crossed yet as (from, to)
        # spans, the read head it was worked out at), one tuple so
        # reanchor_lag_seconds() reads it whole without the lock. The lag
        # takes a re-anchor in when it is made, since the heard sample holds
        # from then on whether or not the write lands; the count and the log
        # line wait for a ring write to land at it. Until then it is also
        # _unlanded_reanchor, (shift in bytes, the log line's span), which the
        # lag leaves out again once the writer has given up (_failed_lag_bytes),
        # and the writer's retries of a failed write go back to its anchor.
        self._reanchors = 0
        self._reanchor_lag: tuple[int, tuple[tuple[int, int], ...], int] = (0, (), 0)
        self._unlanded_reanchor: tuple[int, str] | None = None
        # Odd while the writer is between the read head it re-anchors at and
        # the lag it publishes (_write_payload), so reanchor_lag_seconds() can
        # tell a lag about to be replaced from the current one. Bumped only
        # under _io_lock; there is no ring I/O inside that span.
        self._lag_seq = 0
        self._pushed_samples = 0  # total source samples accepted via push_samples

        # cut() bumps _flush_epoch and flush() then rewrites the lead under
        # _io_lock, and the writer compares a chunk's tag against it under that lock, so
        # a chunk pushed before a splice is discarded or overwritten instead of
        # played past the cut-over. _io_lock
        # also serializes the {_write_wrapped, _written} read-modify-write
        # between flush() (playlist thread) and the writer thread. _output_silenced tracks the pause mute
        # ($DF21 volume 0) so the next flush() restores the channel volume.
        self._flush_epoch = 0
        # The epoch whose ring cut-over flush() has finished. Between the bump
        # and the cut-over the old lead is still in the ring and about to be
        # blanked, so current audio waits for this to catch up rather than be
        # written into it.
        self._cut_epoch = 0
        self._io_lock = threading.Lock()
        self._output_silenced = False
        # Bumped by every start(). A writer carries the generation it was
        # started with and stops writing once that is no longer current, so one
        # still waiting on the queue when stop() and the next start() land
        # cannot write into the new activation's ring.
        self._writer_gen = 0
        # The generation whose writer gave up on the link. That writer touches
        # no ring state, only the channel control register, so a later
        # activation need not refuse while it lingers in a link call.
        self._gave_up_gen: int | None = None
        # Held across the give-up writer's gate-off and start()'s gate-on, so
        # a gate-off still in flight from a retired writer cannot land after
        # the next activation's gate-on and silence it.
        self._gate_lock = threading.Lock()

        # The dead-man deadline (DEADLINE_GUARD_S): the absolute position the
        # channel's length register stops it at, as last confirmed landed;
        # None until a gate-on programs one. _ring_phase is the absolute
        # position at ring offset 0: a restart gates the channel on again,
        # which starts it from offset 0 at the read head of the moment.
        self._deadline: int | None = None
        self._ring_phase = 0
        self._deadline_guard = int(DEADLINE_GUARD_S * self._actual_rate) * self.bps
        # Refreshed once the read head is this close, by at least the step.
        # The ring is kept no less than the low watermark ahead of the reader
        # (a live stream sits there), so that is the distance: a refresh then
        # still finds a step to take before the guard. A decoder a whole lead
        # ahead costs a refresh every 0.75 s, a stream at the watermark one
        # every 1/16 of the lead target.
        self._deadline_refresh = max(self._lead_panic, 3 * self._deadline_guard)
        self._deadline_step = max(self.bps, self._deadline_refresh // 4)
        # The underrun pad keeps the ring a quarter of the lead target ahead
        # of the reader, and the deadline has to stay further ahead than its
        # guard, so a lead under eight guards (0.4 s; only the constructor
        # reaches one) would restart the channel over scheduling jitter. Such
        # a channel loops the whole ring, as it did before the deadline.
        self._uses_deadline = self._lead_target >= 8 * self._deadline_guard
        self._restarts = 0

        self._underrun_pads = 0
        self._lead_min: int | None = None
        self._lead_max: int | None = None

        self._tap_buf = np.zeros(SAMPLE_TAP_SIZE, dtype=np.float32)
        self._tap_write = 0
        self._tap_lock = threading.Lock()

        # Mirrors AudioStreamer.analysis_sink: a reactive source installs its
        # AnalysisTap.push here, and push_samples feeds it mono floats BEFORE
        # any DSP. None (the default) = non-reactive.
        self.analysis_sink: Callable[[np.ndarray], None] | None = None
        self._analysis_sink_failed = False

    @property
    def effective_rate(self) -> float:
        """The rate the FPGA actually clocks samples out at, in Hz.

        Same contract as `AudioStreamer.effective_rate`, so a scene can read
        either sink's real-time rate without caring which one it holds. Here
        it is the exact (unrounded) REF/divider; `sample_rate` is that value
        rounded to an int for the resampler. Note this class has always
        reported the *achieved* rate rather than the request — the DAC path
        only just caught up.
        """
        return self._actual_rate

    def arm(self) -> None:
        """Ready this sampler for a new activation, before its producer starts.

        A scene builds its sampler once and a looping playlist re-runs setup()
        on it, so everything one activation leaves behind is cleared here: the
        stop latch, the EOF latch, the pushed and written totals, the pause
        mute, the telemetry and the tap. The epoch bump disowns anything a
        previous producer still had in flight.

        It is a separate call rather than part of stop() because both callers
        join their producer *after* stopping the sampler (the stop is what
        releases a producer parked on a full queue); clearing the latch in
        stop() would let that producer's last chunks into the next
        activation's prebuffer. `start()` arms by itself when a caller did not,
        but anything pushed in between is dropped, so call this first.

        Raises RuntimeError while the last activation's writer is still alive
        (it outlived stop()'s bounded join), since two writers would share the
        ring and the write head. A writer that had given up on the link is
        let go instead: it no longer touches the ring, and `_gate_lock` keeps
        its last gate-off from landing after the next gate-on."""
        self._refuse_if_writer_survives()
        self._flush_epoch += 1
        while True:
            try:
                self._q.get_nowait()
            except queue.Empty:
                break
        self._stopped = False
        self._failed = False
        self._eof = False
        self._input_ended = False
        # The scene reinstalls its analyzer every activation, so a failure on an
        # earlier one must not leave this activation's failure unlogged.
        self._analysis_sink_failed = False
        self._pushed_samples = 0
        self._carry = None
        with self._io_lock:
            self._written = 0
            self._content_pos = 0
            self._cut_epoch = self._flush_epoch
            self._late_ref = None
            self._burst_start = self._prev_start = None
            self._last_try = None
            self._last_late = None
            self._late_from = None
            self._reanchor_sticky = False
            self._reanchor_lag = (0, (), 0)
            self._unlanded_reanchor = None
            # The read head restarts at 0 with the next gate, so a head kept
            # from the last activation would hold every partial gather until
            # the new one passed it, and a clip shorter than a quantum forever.
            self._last_write_head = None
            self._ring_phase = 0
            self._deadline = None
        self._output_silenced = False
        self._restarts = 0
        self._underrun_pads = 0
        self._late_bytes = 0
        self._reanchors = 0
        self._lead_min = None
        self._lead_max = None
        with self._tap_lock:
            self._tap_buf[:] = 0.0
            self._tap_write = 0

    def _refuse_if_writer_survives(self) -> None:
        writer = self._writer
        if writer is None:
            return
        if writer.is_running() and self._gave_up_gen != self._writer_gen:
            raise RuntimeError(
                "sampler: the previous writer thread is still running; refusing to start another"
            )
        self._writer = None

    def start(self, prebuffer_timeout: float = 2.0, *, hold: bool = False) -> None:
        """Prefill the ring with silence, prebuffer ``_prebuffer_target`` bytes
        of real PCM, then gate the looping channel on.

        With ``hold`` the gate waits for :meth:`release_hold` instead, and
        ``position_seconds`` reads 0 until then: a video scene holds the sound
        until its first frame is on screen, so the clock starts from the
        picture. Whatever the producer pushes meanwhile queues behind the
        prebuffer, and a stop() while held leaves the channel off.

        Prefilling the whole ring with NEUTRAL guarantees the FPGA never reads
        uninitialized REU even under a startup jitter spike; the prebuffer seeds
        the write-ahead lead so the writer starts already ahead of the reader.
        Only the (smaller) prebuffer target is seeded before gating — the writer
        then ramps the lead up to ``_lead_target`` — so playback starts promptly
        while the runtime lead stays deep enough to ride out decode stalls.

        Raises RuntimeError on a sampler that is already running, or whose
        last writer is still alive and had not given up on the link. The
        gate-on waits for a given-up writer's gate-off still in flight, which
        is one write and its flush, each bounded by the transport's own
        timeouts."""
        if self._running or self._held:
            raise RuntimeError("sampler is already started")
        self._refuse_if_writer_survives()
        if self._stopped:
            self.arm()
        self._prefill_neutral()

        prebuf = self._collect_prebuffer(self._prebuffer_target, prebuffer_timeout)
        # No deeper than the runtime lead: one oversized chunk would otherwise
        # be written whole, past the lead and even past the ring. The rest is
        # carried to the writer, so no sample is dropped.
        head = min(len(prebuf), self._lead_target)
        with self._io_lock:
            if head:
                self._confirm("prebuffer write", lambda: self._write_wrapped(0, prebuf[:head]))
            self._written = head
            self._content_pos = head
        if len(prebuf) > head:
            self._carry = (self._flush_epoch, memoryview(prebuf)[head:])

        self._held = True
        if not hold:
            self.release_hold()

    def release_hold(self) -> None:
        """Gate the ring on and start the writer, for a ``start(hold=True)``.
        A no-op when nothing is held, including after stop()."""
        with self._gate_lock:
            if not self._held:
                return
            self._held = False
            self._writer_gen += 1
            gen = self._writer_gen
            # The prefill made the whole first lap NEUTRAL, so the first
            # deadline may sit a lead target in whatever the prebuffer held.
            self._deadline = max(self._written, self._lead_target) if self._uses_deadline else None
            # A voice stopped at a deadline stays in `finished` until a gate-off,
            # and the last stop()'s gate-off may have been lost to the outage
            # that stopped it: a gate-on onto it alone plays nothing.
            self._send_gate_off()
            program_channel(
                self.api,
                self.channel,
                reu_offset=self.ring_base,
                length=self.ring_size
                if self._deadline is None
                else self._deadline_offset(self._deadline),
                rate=self._actual_rate,
                bits=self.bits,
                volume=self._volume,
                pan=self._pan,
                repeat=True,
                repeat_a=0,
                repeat_b=self.ring_size,
                gate=False,
                ref_clock=self._ref_clock,
            )
            # Read before the gate-on goes out, as at a restart: read after
            # its flush, the voice ran ahead of the read head by the round
            # trip and could reach the first deadline unseen.
            self._gate_time = time.monotonic()
            self._send_gate_on()
        self._running = True
        # The loop stops on self._running and its generation, not the PollThread
        # event; the poll supplies only the daemon-thread start/join lifecycle.
        self._writer = PollThread(
            lambda stop: self._writer_loop(gen), name="uaudio-writer", manual=True, join_timeout=1.0
        )
        self._writer.start()
        ref_note = "" if self._ref_clock == SAMPLER_REF_CLOCK else f", ref {self._ref_clock} Hz"
        log.info(
            "sampler: streaming ring up — %d-bit @ %d Hz (div %d, %.2f Hz actual%s), "
            "ring %d KiB @ $%06X, lead %.2f s",
            self.bits,
            self.sample_rate,
            self._divider,
            self._actual_rate,
            ref_note,
            self.ring_size // 1024,
            self.ring_base,
            self._lead_target / self.bps / self._actual_rate,
        )

    def _prefill_neutral(self) -> None:
        block = self._neutral_unit * (REU_WRITE_SLICE // self.bps)

        def prefill() -> None:
            for off in range(0, self.ring_size, len(block)):
                n = min(len(block), self.ring_size - off)
                self.api.reu_write(self.ring_base + off, block[:n])

        self._confirm("ring prefill", prefill)

    def _confirm(self, what: str, write: Callable[[], None]) -> None:
        """``write``, confirmed delivered (``delivery.write_confirmed``). One
        that never confirms is logged and playback goes on: the channel reads
        whatever the REU held there until the writer passes it, which on a
        reused ring is the previous scene's audio."""
        if not write_confirmed(self.api, write):
            log.warning(
                "sampler: the %s was not confirmed delivered after %d attempts; "
                "stale REU audio may play until the writer reaches it",
                what,
                CONFIRM_TRIES,
            )

    def _collect_prebuffer(self, want_bytes: int, timeout: float) -> bytes:
        """Drain at least ``want_bytes`` of queued PCM, blocking up to
        ``timeout`` total for the producer to deliver it (whatever arrived by
        then is used — the writer fills the rest ahead of the reader, NEUTRAL
        on underrun). Returns **all** collected bytes (never truncated — a
        partial-chunk truncation would drop samples and glitch the stream).

        Stops early once ``end_input()`` has been called and the queue is
        empty: a clip shorter than the prebuffer has nothing more to deliver."""
        deadline = time.monotonic() + timeout
        chunks: list[bytes] = []
        have = 0
        while have < want_bytes:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            # Read before the get: end_input() follows the producer's last put,
            # so a get that times out after it was seen leaves nothing behind.
            ended = self._input_ended
            try:
                epoch, chunk = self._q.get(timeout=min(remaining, _PREBUFFER_POLL_S))
            except queue.Empty:
                if ended:
                    break
                continue
            if epoch != self._flush_epoch:
                continue
            chunks.append(chunk)
            have += len(chunk)
        return b"".join(chunks)

    def push_samples(self, samples_int16: np.ndarray, *, epoch: int | None = None) -> int:
        """Accept mono int16 from the demuxer; encode + enqueue for the writer.

        Blocks when the queue is full so PyAV naturally throttles to the
        playback rate (same backpressure as the DAC's ``push_samples``).
        ``epoch`` is the flush epoch (:meth:`current_flush_epoch`) the
        producer read alongside its decision to push; without one, the epoch
        at entry. Returns the samples accepted: 0 once stopped, after the
        writer has given up on the link, or when a splice made the chunk
        stale."""
        if self._stopped or self._failed:
            return 0
        # The chunk carries the epoch it was produced in. A splice that lands
        # while this call waits on a full queue makes the put pointless, so the
        # bounded put timeout re-checks; a put that lands just before the splice
        # is dropped by the writer on its stale tag.
        if epoch is None:
            epoch = self._flush_epoch
        raw = samples_int16.astype(np.float32) / _INT16_FULL_SCALE
        self._tap_push(raw)
        floats = raw
        if self._dsp is not None and self._dsp.active:
            floats = self._dsp.process(floats)
        out_i16 = np.clip(np.rint(floats * 32767.0), -32768, 32767).astype(np.int16)
        pack = pack_pcm(out_i16, self.bits)
        while not (self._stopped or self._failed):
            if self._flush_epoch != epoch:
                return 0
            try:
                self._q.put((epoch, pack), timeout=0.1)
                break
            except queue.Full:
                continue
        else:
            return 0
        # After a successful put of a still-current chunk only: a dropped chunk
        # must not inflate position_seconds's pushed-total EOF ceiling, nor
        # enter the analysis tap, which a file source reads at the slot each
        # sample was written for (pre-DSP, for parity with
        # AudioStreamer.push_samples).
        if self._flush_epoch != epoch:
            return 0
        accepted = int(samples_int16.shape[0])
        self._pushed_samples += accepted
        # A video's demuxer ends its input at EOF and pushes again after
        # a seek back (an A/B loop wrap, a resume near the end).
        self._input_ended = False
        self._push_to_analysis(raw)
        return accepted

    def end_input(self) -> None:
        """The ``push_samples`` producer has ended: ``start()`` gates the ring
        on what it pushed rather than waiting out the prebuffer timeout for
        audio that will not come. Call it after the last push returns, and
        again after a later push: the next accepted push, or a splice's
        cut() while the sampler runs, reopens the input. Leaves ``position_seconds`` alone, unlike ``mark_eof``."""
        self._input_ended = True

    def mark_eof(self) -> None:
        """Source exhausted — clamp ``position_seconds`` to the pushed total,
        plus the re-anchor lag it plays behind, so an over-running wall clock
        can't desync the (now-ended) video."""
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

    def _write_volume(self, value: int) -> None:
        """Write channel volume (0..63) live, without reprogramming the channel.
        Used by the pause fast mute ($DF21 for channel 0) and its restore."""
        addr = channel_base(self.channel) + REG_VOLUME
        self.api.write_memory(f"{addr:04X}", f"{value & 0x3F:02X}")
        self.api.flush()

    def current_flush_epoch(self) -> int:
        """The flush epoch a push made now is tagged with; see
        :meth:`push_samples`."""
        return self._flush_epoch

    def cut(self) -> FlushCut:
        """Retire everything pushed so far and anchor the splice: the first
        step of :meth:`flush`, split out so a video source can take it under
        the lock that sets its pending seek (see
        :meth:`AVFileSource.request_seek`). The writer holds back audio of
        the new epoch until the matching ``flush(cut=...)`` has rewritten the
        ring, so every cut must be followed by that flush.

        The post-splice anchor is the read head now, one margin past it. The
        volume write and the wait for _io_lock in flush() (the writer holds
        it for a whole REU write, up to a slice, about 60 ms) come after, and
        an anchor taken past them would put the sound that much behind the
        picture. Audio whose slot that wait used up is dropped as late."""
        if not self._running:
            return FlushCut(None, self.position_seconds() + self.ring_lead_seconds())
        anchor = self._read_consumed_bytes() + self._flush_margin
        # Not under _io_lock: the writer holds it for a whole REU write, and the
        # demuxer may apply the seek and push post-splice audio meanwhile, which
        # an epoch bumped late would tag stale and drop. A stale chunk the
        # writer has already passed its check for is written under the lock
        # before the cut-over takes it, and that cut-over rewrites it.
        epoch = self._flush_epoch + 1
        self._flush_epoch = epoch
        # An end the pre-splice pass marked is not the post-splice input's:
        # left set, the writer stops counting a stall after the splice.
        self._input_ended = False
        return FlushCut(epoch, anchor / self.bps / self._actual_rate, anchor)

    def flush(self, *, silence_output: bool = False, cut: FlushCut | None = None) -> float:
        """Cut the ring over to post-splice audio: retire everything queued
        (by bumping the flush epoch, :meth:`cut`) and NEUTRAL-rewrite the
        unconsumed lead past a small guard margin, then pull the write head
        back to consumed+margin. The first post-splice sample is anchored one
        margin past the read head as of the cut (`ring_lead_seconds()`
        reports that margin). ``cut`` is one already taken, else this takes
        it. Used by VideoScene's transport splice (seek / loop wrap / resume)
        so stale pre-splice audio doesn't play after the demuxer re-seeks.
        ``position_seconds()`` (wall-based) is unaffected — the read head keeps
        advancing, we only change what it reads.

        Returns the splice anchor: the ``position_seconds()`` at which the
        first post-splice sample is heard, on the clock as it runs after the
        flush, which clears ``mark_eof``'s clamp.

        ``silence_output`` (pause) additionally writes channel volume 0 for
        instant silence independent of ring content; the next plain ``flush()``
        (resume's splice) restores it. Present on the DAC's ``flush()`` too for
        signature parity — this ring cut-over already silences within
        ``FLUSH_GUARD_S`` regardless."""
        if cut is None:
            cut = self.cut()
        if cut.epoch is None:
            return cut.anchor_s
        try:
            self._cut_over(cut.ring_pos, cut.epoch, silence_output=silence_output)
        except BaseException:
            # No cut-over is coming: let the writer go on from where it was.
            self._cut_epoch = max(self._cut_epoch, cut.epoch)
            raise
        return cut.anchor_s

    def _cut_over(self, anchor: int, epoch: int, *, silence_output: bool) -> None:
        """flush() after its epoch bump: the volume write, then the ring
        rewrite, which releases audio of ``epoch`` to the writer."""
        # Under _gate_lock: a restart programs the volume it read from
        # _output_silenced, and a restore landing between that read and its
        # write was overwritten, leaving the channel muted after the resume.
        with self._gate_lock:
            if silence_output:
                self._write_volume(0)
                self._output_silenced = True
            elif self._output_silenced:
                self._write_volume(self._volume)
                self._output_silenced = False
        # The queue is not drained: the writer and the prebuffer drop stale
        # tags, and a drain would also take post-splice audio pushed since the
        # bump above, losing the start of the seek target.
        with self._io_lock:
            consumed = self._read_consumed_bytes()
            new_written = consumed + self._flush_margin
            # Nothing behind the read head is worth rewriting, and no rewrite
            # spans more than the ring: a writer stalled far behind would
            # otherwise make this one write without bound under the lock.
            lo = max(min(self._written, new_written), consumed)
            hi = max(self._written, new_written)
            lo = max(lo, hi - self.ring_size)
            if hi > lo:
                # One formula covers both the normal rewrite-the-lead case
                # (new_written < old _written: blank [consumed+margin, old W))
                # and the rare lead<margin case (new_written > old _written:
                # blank the lap-stale region the reader is about to enter).
                self._blank(lo, hi)
            self._written = new_written
            self._content_pos = anchor
            # The splice re-aligns sound and picture, and its own late drops
            # (the demuxer's re-seek delay) start a fresh window.
            self._late_ref = None
            self._burst_start = self._prev_start = None
            self._last_late = None
            self._late_from = None
            self._reanchor_sticky = False
            self._reanchor_lag = (0, (), 0)
            self._unlanded_reanchor = None
            self._eof = False
            self._cut_epoch = max(self._cut_epoch, epoch)

    def _writer_loop(self, gen: int) -> None:
        """Run writer steps until stopped, superseded, or the link is given up.

        A step that raises (a REU write the link could not deliver) is
        retried after a doubling back-off rather than ending the thread: a
        dead writer leaves the channel gated (looping the ring's stale audio
        when it has no deadline) while the producer parks on a queue nothing
        drains. Past
        WRITER_GIVE_UP_S of unbroken failure it gates the channel off, and
        stays to retry that until it lands."""
        failing_since: float | None = None
        backoff = 0.0
        while self._running and gen == self._writer_gen:
            try:
                wrote = self._writer_step(gen)
            except Exception as e:
                now = time.monotonic()
                if failing_since is None:
                    failing_since = now
                    log.warning("sampler: ring write failed (%s); retrying", e)
                elif now - failing_since >= WRITER_GIVE_UP_S:
                    self._give_up(e, gen)
                    return
                backoff = min(WRITER_BACKOFF_MAX_S, max(WRITER_BACKOFF_MIN_S, backoff * 2))
                time.sleep(backoff)
                continue
            if wrote and failing_since is not None:
                log.info(
                    "sampler: ring writes recovered after %.1f s",
                    time.monotonic() - failing_since,
                )
                failing_since = None
                backoff = 0.0

    def _give_up(self, error: Exception, gen: int) -> None:
        """Stop taking audio, and gate the channel off once the link carries
        the write.

        The gate-off goes over the link that just failed, and `write_memory`
        does not raise when a write is lost, so it is confirmed against
        `delivery_epoch` and sent again every WRITER_BACKOFF_MAX_S until it
        lands or the writer is stopped or superseded. Sent once, it was lost
        to the outage it answered, and the ring went on looping stale audio
        after the link came back, under a scene that survives the outage."""
        self._failed = True
        self._gave_up_gen = gen
        log.error(
            "sampler: ring writes failing for %.0f s (%s); gating the channel off",
            WRITER_GIVE_UP_S,
            error,
        )
        retrying = False
        while True:
            landed = self._gate_off_landed(gen)
            if landed is None:
                return
            if landed:
                if retrying:
                    log.info("sampler: the link is back; channel gated off")
                return
            if not retrying:
                retrying = True
                log.warning(
                    "sampler: the gate-off did not reach the machine; "
                    "retrying until the link answers"
                )
            time.sleep(WRITER_BACKOFF_MAX_S)

    def _gate_off_landed(self, gen: int) -> bool | None:
        """One gate-off, and whether the link vouches it arrived: nothing it
        carried was counted lost (``delivery_epoch`` unmoved). A loss of some
        other thread's write in the same window reads as this one's, which
        costs a repeat of an idempotent write. None, with nothing sent, once
        the writer is stopped or superseded.

        The write is not flushed once ``delivery_epoch`` has moved: nothing
        is left to confirm, and `flush` logs a warning per failure, which at
        one retry every WRITER_BACKOFF_MAX_S floods the log for as long as
        the outage lasts."""
        with self._gate_lock:
            if not (self._running and gen == self._writer_gen):
                return None
            epoch = self.api.delivery_epoch
            try:
                self.api.write_memory(f"{channel_base(self.channel):04X}", "00")
                if self.api.delivery_epoch != epoch:
                    return False
                self.api.flush()
            except Exception as e:  # the link is what failed
                log.debug("sampler: gate-off raised: %s", e)
                return False
            return self.api.delivery_epoch == epoch

    def _writer_step(self, gen: int) -> bool:
        """One writer pass: restart a channel that ran into its deadline,
        else a ring pass (`_ring_step`) and then the deadline moved up behind
        what it wrote. Returns whether it wrote. Raises when the link lost a
        write, like the ring writes themselves, except a refresh lost in a
        pass whose ring write went out: that one is retried on the next pass."""
        deadline = self._deadline
        if deadline is not None and self._read_consumed_bytes() + self._deadline_guard >= deadline:
            return self._restart_channel(gen)
        wrote = self._ring_step(gen)
        try:
            self._advance_deadline(gen)
        except ConnectionError as e:
            if not wrote:
                raise
            # The ring write landed, and a lost write on another thread reads
            # as this refresh's loss, so failing the pass would count toward a
            # give-up on a link that carries the ring. The old deadline stands:
            # the next pass retries, and reaching it restarts the channel.
            log.debug("sampler: %s; retrying next pass", e)
        return wrote

    def _ring_off(self, pos: int) -> int:
        """The ring offset that holds absolute byte position ``pos``."""
        return (pos - self._ring_phase) % self.ring_size

    def _deadline_offset(self, pos: int) -> int:
        """The length register's value for a deadline at ``pos``. Offset 0 is
        never matched (the position has moved past it before the first
        comparison), so a deadline is never placed there (`_next_deadline`)."""
        off = self._ring_off(pos)
        if off == 0:
            raise ValueError(f"sampler: no deadline can sit at ring offset 0 (position {pos})")
        return off

    def _send_gate_off(self) -> None:
        """Clear the channel's control register, unflushed: the
        `program_channel` that follows flushes it with the registers."""
        self.api.write_memory(f"{channel_base(self.channel):04X}", "00")

    def _send_gate_on(self) -> None:
        """Gate the looping ring on and flush: the step `program_channel`
        takes last, split out so the caller reads the clock just before it."""
        ctrl = control_byte(gate=True, repeat=True, bits=self.bits)
        self.api.write_memory(f"{channel_base(self.channel):04X}", f"{ctrl:02X}")
        self.api.flush()

    def _write_length(self, offset: int) -> None:
        addr = channel_base(self.channel) + REG_LENGTH
        self.api.write_regs(f"{addr:04X}", *_be_bytes(offset, 3))

    def _advance_deadline(self, gen: int) -> None:
        """Move the deadline up to what the ring holds, once the read head is
        within `_deadline_refresh` of it. Confirmed against
        `delivery_epoch`: a lost refresh raises, and the writer backs off and
        tries again. A refresh that may have landed after the channel reached
        the old deadline leaves the old one standing, so the next pass
        restarts the channel: one that stopped silently would stay silent."""
        old = self._deadline
        if old is None:
            return
        consumed = self._read_consumed_bytes()
        target = self._written
        if old - consumed >= self._deadline_refresh or target - old < self._deadline_step:
            return
        new = self._next_deadline(old, target, consumed)
        if new is None:
            return
        with self._gate_lock:
            if not (self._running and gen == self._writer_gen):
                return
            epoch = self.api.delivery_epoch
            self._write_length(self._deadline_offset(new))
            if self.api.delivery_epoch == epoch:
                self.api.flush()
            if self.api.delivery_epoch != epoch:
                raise ConnectionError("sampler: the link lost a deadline write")
            if self._read_consumed_bytes() + self._deadline_guard >= old:
                return
            self._deadline = new

    def _next_deadline(self, old: int, target: int, consumed: int) -> int | None:
        """The furthest deadline in ``(old, target]`` whose write is safe, or
        None. Safe means no value the register passes through on the way
        (`length_write_intermediates`) is where the FPGA may be playing: from
        a flush margin behind the read head (drift) to two guards ahead.

        Tried in turn: ``target`` itself, the end of the 64 KB block ``old``
        sits in, and the start of the next one. A write that crosses a block
        changes the high byte, and a mix of old and new bytes lands near
        ``target`` less 64 KB, which is the read head when the lead is about
        64 KB (32 kHz/16-bit at 1 s); stopping at the block's end and then
        stepping onto the next block's start mixes to values near that
        block's start instead, which is the read head only for a lead of
        about 128 KB. One of the three is always clear of it."""
        ring = self.ring_size
        o = self._ring_off(old)
        block_end = min(o | 0xFFFF, ring - 1)
        block_end -= block_end % self.bps
        next_block = (o | 0xFFFF) + 1
        candidates = [
            target - self.bps if self._ring_off(target) == 0 else target,
            old + block_end - o,
            old + ((next_block if next_block < ring else self.bps) - o) % ring,
        ]
        for new in candidates:
            if old < new <= target and self._length_safe(o, self._ring_off(new), consumed):
                return new
        return None

    def _length_safe(self, old_off: int, new_off: int, consumed: int) -> bool:
        ring = self.ring_size
        behind = self._flush_margin
        window = behind + 2 * self._deadline_guard
        lo = self._ring_off(consumed) - behind
        for value in length_write_intermediates(old_off, new_off):
            # Never matched: the loop wraps first at ring_size, and the first
            # comparison after a wrap or a gate-on is already past offset 0.
            if value == 0 or value >= ring:
                continue
            if (value - lo) % ring <= window:
                return False
        return True

    def _restart_channel(self, gen: int) -> bool:
        """Gate a channel that reached its deadline on again, at the read
        head: the gate-on starts it from ring offset 0, so `_ring_phase`
        moves to the read head just before the gate-on and the ring holds nothing
        for the new phase yet. A lead target of it is blanked before the
        gate-on and the deadline set at its end, as at the first gate-on;
        the writer's audio overwrites the blank, and what is late is
        dropped. Raises when the link lost any of it, so the writer backs
        off and tries again.

        The blank goes first: on a dead link `reu_write` raises at once,
        where a register write is lost quietly and its flush logs a warning,
        one per retry for as long as the outage lasts."""
        with self._gate_lock:
            if not (self._running and gen == self._writer_gen):
                return False
            with self._io_lock:
                epoch = self.api.delivery_epoch
                fresh = self._lead_target
                self._write_wrapped(0, self._neutral_unit * (fresh // self.bps))
                # `finished` is left only through a gate-off; the next gate-on
                # starts the channel from offset 0.
                self._send_gate_off()
                program_channel(
                    self.api,
                    self.channel,
                    reu_offset=self.ring_base,
                    length=fresh,
                    rate=self._actual_rate,
                    bits=self.bits,
                    volume=0 if self._output_silenced else self._volume,
                    pan=self._pan,
                    repeat=True,
                    repeat_a=0,
                    repeat_b=self.ring_size,
                    gate=False,
                    ref_clock=self._ref_clock,
                )
                # Read before the gate-on goes out, not after its flush: the
                # FPGA then runs behind the read head by the gate-on's send
                # rather than ahead of it by the round trip, where a slow link
                # let it reach the deadline unseen and stop with a refresh
                # still taken as on time. Read before the gate-off, the lag
                # took in the register flush too, a redial after an outage
                # among it, and the sound played that much behind the picture.
                phase = self._read_consumed_bytes()
                self._send_gate_on()
                if self.api.delivery_epoch != epoch:
                    raise ConnectionError("sampler: the link lost the channel restart")
                self._ring_phase = phase
                self._written = phase + fresh
                self._deadline = phase + fresh
                self._restarts += 1
                restarts = self._restarts
        log.log(
            logging.WARNING if restarts == 1 else logging.DEBUG,
            "sampler: the channel reached its deadline (ring writes stopped landing); "
            "restarted it at the read head%s",
            "" if restarts == 1 else f" (restart {restarts})",
        )
        return True

    def _ring_step(self, gen: int) -> bool:
        """One ring pass: sleep while far enough ahead, else write the next
        chunk at its anchored position, or an underrun pad. Returns whether it
        wrote to the ring."""
        if self._cut_epoch != self._flush_epoch:
            # A flush() is between its bump and its cut-over; anything written
            # now lands in the lead that cut-over blanks.
            time.sleep(0.002)
            return False
        consumed = self._read_consumed_bytes()
        # The real-audio cushion: pads ahead of _content_pos do not count, so
        # the writer keeps pulling data to overwrite them.
        lead = self._content_pos - consumed
        # The stop() summary reports what the ring holds ahead of the reader,
        # pads included: the cushion against the reader running dry. The
        # content lead above goes far negative while audio is late or paused,
        # which says nothing about the ring. (An unlocked read; telemetry.)
        ahead = self._written - consumed
        self._lead_min = ahead if self._lead_min is None else min(self._lead_min, ahead)
        self._lead_max = ahead if self._lead_max is None else max(self._lead_max, ahead)
        room = self._lead_target - lead
        if room < self._write_quantum:
            # Far enough ahead. The bounded queue + blocking push give the
            # producer backpressure, so the lead can't run away.
            time.sleep(0.002)
            return False
        # A producer paced at real time (a live stream) never builds a queue
        # backlog, so writing what one pass gathered would write every frame.
        # While the lead has the slack, the writer waits for a whole quantum.
        # Read before the gather: end_input() follows the producer's last put,
        # so a gather that comes back empty after it was seen leaves nothing
        # behind in the queue.
        ended = self._input_ended
        payload = self._next_payload(room)
        if payload is None:
            if self._carry is not None:
                return False  # held for a whole quantum: data is flowing
            return self._pad_underrun(gen, stalled=not ended)
        return self._write_payload(gen, *payload)

    def _write_payload(self, gen: int, epoch: int, data: bytes) -> bool:
        """Write ``data`` at its anchored position, dropping its leading bytes
        whose slot is already within FLUSH_GUARD_S of the reader (they are
        late, and a write there would race the FPGA's fetch). Serialized
        against flush()'s cut-over; the generation is re-checked because a
        stop() and the next start() can both land while this thread waits on
        the queue."""
        with self._io_lock:
            if gen != self._writer_gen or epoch != self._flush_epoch:
                return False
            if epoch != self._cut_epoch:
                # Current audio whose cut-over has not run yet (the flush bumped
                # after this pass's check above): held for the new anchor.
                self._carry_back(epoch, data)
                return False
            before = self._content_pos
            last_try, last_late = self._last_try, self._last_late
            self._lag_seq += 1  # odd: a re-anchor's head and lag are in flight
            try:
                consumed = self._read_consumed_bytes()
                c = self._late_anchor(consumed)
            finally:
                self._end_lag_window()
            # _writer_step sized this payload to the room under the lead target
            # at the old _content_pos; a re-anchor moved it forward, so the tail
            # past the target waits for the next pass. A lead too shallow to
            # leave any room past _reanchor_lead (keep 0) writes it whole: carried,
            # it would be re-anchored to no room again on every pass, and nothing
            # would ever land.
            keep = consumed + self._lead_target - c
            if c != before and 0 < keep < len(data):
                self._carry_back(epoch, data[keep:])
                data = data[:keep]
            end = c + len(data)
            first = max(c, consumed + self._flush_margin)
            if first < end:
                try:
                    # A write head the reader passed (a link stall longer than
                    # the lead) leaves stale ring bytes just ahead of it; blank
                    # them rather than let the reader replay a lap-old span.
                    # The blank rides in the same write as the audio after
                    # it: a late gather's dropped head leaves that span, and
                    # a write of its own for each one took the second after
                    # a seek to 72 REU writes (fake clock, 44.1 kHz, 2.5 ms
                    # frames), and 65-67 on hardware.
                    gap = max(self._written, consumed)
                    pos, payload = first, data[first - c :]
                    if gap < first:
                        blank = self._neutral_unit * ((first - gap) // self.bps)
                        pos, payload = gap, blank + payload
                    self._write_wrapped(self._ring_off(pos), payload)
                except Exception:
                    # Retried at the same anchor on the next pass: rewriting
                    # the slices that did land is idempotent. A link outage says
                    # nothing about the producer, so a failed attempt does not
                    # feed the trend rule: the window restarts once writes land
                    # again, and the outage counts as a gap with no attempt.
                    # Timed across the retries' back-off, the lateness grew and
                    # re-anchored a backlog that would have lined up at once.
                    # A re-anchor this attempt made keeps its anchor, so the
                    # retry rewrites the same slots, but stays unlanded
                    # (_land_reanchor): counted at once, an outage's retries
                    # put content_lag_seconds past audio that never landed,
                    # which an audio-file scene waited out on silence once the
                    # writer gave up. Rolled back to the anchor before it, a
                    # retry of a sticky producer re-anchored afresh past the
                    # slices that did land, and the reader played their audio
                    # twice (fake link: 10 bytes of a 40-byte write split at
                    # the ring's end, retried 5 ms later).
                    self._late_ref = None
                    self._burst_start = self._prev_start = None
                    self._last_try, self._last_late = last_try, last_late
                    self._carry_back(epoch, data)
                    raise
                self._written = max(self._written, end)
                self._last_write_head = consumed
                self._land_reanchor()
            self._late_bytes += min(len(data), max(0, first - c))
            self._content_pos = end
            return first < end

    def _gaining(self, then: tuple[int, int], now: tuple[int, int]) -> bool:
        """Whether lateness went from ``then`` to ``now`` (each (read head,
        lateness)) fast enough to be catching up: at a pace that, from
        ``then``, lines up within LATE_CATCHUP_S, and by more than
        MIN_WRITE_INTERVAL_S: the floor holds a gather's head up to that long,
        so lateness measured at the write swings by as much with no change in
        the producer. The interval is a minimum, not a deduction: taken off
        the shrinkage, it re-anchored a decoder lining up at 1.2x from 0.35 s
        late (1.75 s to line up), whose window gains only 12 ms more than
        the pace asks."""
        (t0, l0), (t1, l1) = then, now
        shrink = l0 - l1
        return shrink > self._write_interval and shrink * self._late_catchup_bytes > l0 * (t1 - t0)

    def _late_anchor(self, consumed: int) -> int:
        """Under _io_lock: where the next real sample is written. That is
        _content_pos unless the producer is not keeping up, in which case the
        audio is re-anchored _reanchor_lead past the read head (moving
        _content_pos). The re-anchor's lag is published here; its count and
        log line wait for a ring write to land at it (_land_reanchor).

        The question is the trend of the lateness L (how far the anchor sits
        behind the write floor), not how long writes have been late. A
        producer catching up shrinks L and is left to line up; one that has
        been late for LATE_REANCHOR_S of read-head time without L shrinking
        at a pace that lines up within LATE_CATCHUP_S is not catching up
        (_gaining).

        Within a burst of attempts, L is compared across that window. A gap
        of the window with no attempt at all says nothing until the producer
        delivers again (L can only have grown across it), so it starts a new
        window rather than ending one. Across gaps, the lateness at the start
        of one burst is compared with the start of the burst before it, once
        the gap after the later one shows it did not catch up: a producer
        delivering in bursts seconds behind (a segmented live stream) is no
        closer (or barely closer) from one burst to the next and is
        re-anchored, while a decoder that stalls gets the burst after the
        stall to catch up in, whatever the chunks before the stall did. The comparison waits for that
        second burst because at the first one after a gap the two cannot be
        told apart. After one re-anchor, late audio is re-anchored at once
        until the next splice or arm().

        Only a write more than MIN_WRITE_INTERVAL_S on time ends a window.
        The floor holds a gather's head up to that long, so a live stream
        delivering at the write floor, as one does after a splice, lands
        some writes just on time and the rest late; each of those on-time
        writes ended the window, and the stream dropped audio for a second
        window or more before it was re-anchored. A LATE_REANCHOR_S of such
        writes with none late does end it, as a write further on time would:
        a window must not outlive the lateness that opened it, or one late
        write after 10 s of them re-anchored at once."""
        c = self._content_pos
        lateness = consumed + self._flush_margin - c
        last, self._last_try = self._last_try, consumed
        if lateness <= 0:
            # On time by less than the floor can hold a gather ends nothing
            # until no attempt has been late for a whole window.
            stale = (
                self._last_late is not None
                and consumed - self._last_late >= self._late_reanchor_bytes
            )
            if lateness <= -self._write_interval or stale:
                self._late_ref = None
                self._burst_start = self._prev_start = None
                self._last_late = None
                self._late_from = None
            return c
        self._last_late = consumed
        if self._late_from is None:
            self._late_from = consumed
        late_from = self._late_from
        gap = last is not None and consumed - last >= self._late_reanchor_bytes
        here = (consumed, lateness)
        if self._reanchor_sticky:
            pass  # already shown slow since the last splice: no window
        elif gap:
            prev, ended = self._prev_start, self._burst_start
            self._prev_start, self._burst_start = ended, here
            self._late_ref = here
            if prev is None or ended is None or self._gaining(prev, ended):
                return c
            # The burst that just ended began too little closer than the one
            # before it to be catching up.
        elif self._late_ref is None:
            self._late_ref = self._burst_start = here
            return c
        else:
            if consumed - self._late_ref[0] < self._late_reanchor_bytes:
                return c
            if self._gaining(self._late_ref, here):  # a new window
                self._late_ref = here
                return c
        anchor = consumed + self._reanchor_lead
        shift = anchor - c
        self._late_ref = None
        self._burst_start = self._prev_start = None
        self._last_late = None
        self._late_from = None
        # A sticky re-anchor did not wait out a window, so its log line does
        # not claim one.
        if self._reanchor_sticky:
            late_for = "again"
        else:
            late_for = f"for {(consumed - late_from) / self.bps / self._actual_rate:.1f} s"
        self._reanchor_sticky = True
        # The lag takes this shift in now (the heard sample holds from here
        # whether the write lands or not), and only this one's: an unlanded
        # one before it is in already.
        self._reanchor_lag = self._lag_after_reanchor(consumed, c, shift)
        self._end_lag_window()
        # A re-anchor still waiting on a failed write moved the anchor this one
        # moves on from: they land as one, logged with the first one's span.
        unlanded = self._unlanded_reanchor
        if unlanded is not None:
            shift += unlanded[0]
            late_for = unlanded[1]
        self._unlanded_reanchor = (shift, late_for)
        self._content_pos = anchor
        return anchor

    def _land_reanchor(self) -> None:
        """Under _io_lock, once a ring write has landed: count the re-anchor
        it landed at, if any, into the activation's count, and log it. The lag
        took it in when it was made (_late_anchor)."""
        unlanded = self._unlanded_reanchor
        if unlanded is None:
            return
        _, late_for = unlanded
        self._reanchors += 1
        self._unlanded_reanchor = None
        if self._reanchors == 1:
            level = logging.WARNING
            note = ""
        else:
            # A steadily slow producer re-anchors every few seconds; stop()
            # reports the count.
            level = logging.DEBUG
            note = f" (re-anchor {self._reanchors})"
        log.log(
            level,
            "sampler: audio arrived late %s and is not catching up; "
            "re-anchored at the read head%s — sound now lags the picture by %.2f s",
            late_for,
            note,
            self._reanchor_lag[0] / self.bps / self._actual_rate,
        )

    def _pad_underrun(self, gen: int, *, stalled: bool = True) -> bool:
        """The queue came up empty. Below the low watermark the ring is about
        to run out of anything current, so NEUTRAL-pad ahead of it — a real
        underrun, where the alternative is the FPGA replaying stale ring data.
        The pad leaves _content_pos alone, so the data that follows overwrites
        whatever of it has not been played.

        Counted only when ``stalled``: after end_input() the queue is drained,
        and the pad is the silence the ring plays out after the last sample."""
        with self._io_lock:
            if gen != self._writer_gen:
                return False
            consumed = self._read_consumed_bytes()
            if self._written - consumed > self._lead_panic:
                return False
            lo = max(self._written, consumed)
            hi = min(lo + REU_WRITE_SLICE, consumed + self._lead_target)
            hi -= (hi - lo) % self.bps
            if hi <= lo:
                return False
            if stalled:
                self._underrun_pads += 1
            self._blank(lo, hi)
            self._written = hi
            return True

    def _carry_back(self, epoch: int, data: bytes) -> None:
        carry = self._carry
        if carry is not None and carry[0] == epoch:
            data += bytes(carry[1])
        self._carry = (epoch, memoryview(data))

    def _next_payload(self, room: int) -> tuple[int, bytes] | None:
        """Writer-thread only: the next current-epoch PCM to write, at most
        ``room`` bytes and one slice. Queued chunks are coalesced up to the
        write quantum; a chunk larger than the write is split, its tail
        carried to the next pass. Less than a quantum is carried rather than
        written until the lead comes down to the hold's deadline, and never
        sooner than MIN_WRITE_INTERVAL_S after the previous audio write
        (_holds).
        None when nothing current arrived within the queue timeout, or when it
        was held."""
        limit = min(room, REU_WRITE_SLICE)
        limit -= limit % self.bps
        parts: list[bytes | memoryview] = []
        size = 0
        epoch = -1
        waited = False
        while size < self._write_quantum:
            item: tuple[int, bytes | memoryview]
            if self._carry is not None:
                item, self._carry = self._carry, None
            else:
                # One bounded wait per pass, even behind a held carry, so a
                # writer holding a partial quantum does not spin. A carry that
                # is already due is not held, so it does not wait: the reader
                # would take up to that wait's 20 ms of it as late.
                block = not waited and (not parts or self._holds(size))
                try:
                    item = self._q.get(timeout=0.02) if block else self._q.get_nowait()
                except queue.Empty:
                    break
                waited = True
            if item[0] != self._flush_epoch:
                continue
            if item[0] != epoch:  # a flush landed mid-gather: start over
                parts, size, epoch = [], 0, item[0]
            parts.append(item[1])
            size += len(item[1])
        if not parts:
            return None
        # Read after the queue wait above, not at the start of the pass: the
        # reader moved up to that wait's 20 ms meanwhile, and a hold decided
        # on the older lead wrote its gather late when the producer stalled.
        if self._holds(size):
            self._carry = (epoch, memoryview(b"".join(parts)))
            return None
        whole = memoryview(parts[0]) if len(parts) == 1 else memoryview(b"".join(parts))
        if len(whole) > limit:
            self._carry = (epoch, whole[limit:])
            whole = whole[:limit]
        return epoch, bytes(whole)

    def _holds(self, size: int) -> bool:
        """Writer-thread only: whether a ``size``-byte gather is carried to
        wait for the rest of its quantum, on the read head as of now. It is
        while the lead is above the hold's deadline (the write floor plus
        HOLD_GUARD_S, one queue wait and a write), and in any case until
        MIN_WRITE_INTERVAL_S has passed since the previous audio write.

        The deadline is a deadline, not a forecast: the writer used to hold
        only while the rest of the quantum, arriving at real time, would
        still beat it, so a producer slightly slower than real time wrote
        every 2.5 ms frame on its own for the last ~50 ms of its cushion. The
        interval is the one rule that bounds the write count whatever state
        the lead is in (MIN_WRITE_INTERVAL_S)."""
        if size >= self._write_quantum:
            return False
        consumed = self._read_consumed_bytes()
        if self._content_pos - consumed > self._hold_deadline:
            return True
        last = self._last_write_head
        return last is not None and consumed - last < self._write_interval

    def _hold_threshold(self, size: int) -> int:
        """The content lead from which a held ``size``-byte gather fills its
        quantum at real time before the hold's deadline (_holds)."""
        return self._hold_deadline + (self._write_quantum - size)

    def _blank(self, lo: int, hi: int) -> None:
        """NEUTRAL-write the absolute byte span [lo, hi) of the ring."""
        self._write_wrapped(self._ring_off(lo), self._neutral_unit * ((hi - lo) // self.bps))

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

    def position_seconds(self) -> float:
        """Wall-clock seconds since the ring was gated on — the read head (same
        contract as ``AudioStreamer.position_seconds`` in REU-pump mode); the
        heard position is this less `reanchor_lag_seconds()`, which
        `audio_source.heard_seconds` takes off for every A/V reader. Clamped
        after EOF to the pushed total plus `content_lag_seconds`, where the
        last pushed sample is heard, so the heard position stops at the pushed
        total. A video scene calls `mark_eof` only once it has shown its last
        frame; clamped at the total alone, the heard position (and a transport
        position read off it) then stepped back by that lag. The FPGA crystal
        vs the host monotonic clock differ by ~ppm, so this is drift-free for
        A/V sync."""
        if not self._running:
            return 0.0
        elapsed = time.monotonic() - self._gate_time
        if self._eof and self._pushed_samples:
            total_s = self._pushed_samples / self._actual_rate + self.content_lag_seconds
            return max(0.0, min(elapsed, total_s))
        return max(0.0, elapsed)

    @property
    def content_lag_seconds(self) -> float:
        """How far this activation's re-anchors have put the sound behind
        `position_seconds()`, once the read head has crossed them: every
        shift `reanchor_lag_seconds()` counts, without its holds. Late audio
        is re-anchored past the read head (`_late_anchor`), and the clock
        does not follow it, so the last sample of a track is heard this long
        after the clock reaches the track's length. Cleared by arm() and by a
        splice.

        A re-anchor whose write has not landed yet counts too: the writer
        retries at its anchor, so the sound will lag by it once the link is
        back, and left out, an audio-file scene's end read the clock as
        caught up and cut the scene during the outage. Once the writer has
        given up nothing more lands, and only what did counts
        (`_failed_lag_bytes`). Read without _io_lock, which the writer holds
        for a whole REU write."""
        lag = self._reanchor_lag[0] - self._failed_lag_bytes()
        return max(0, lag) / self.bps / self._actual_rate

    def _failed_lag_bytes(self) -> int:
        """The shift of a re-anchor that never landed because the writer gave
        up on the link: nothing more lands, so the sound lags by none of it,
        and both lag figures leave it out. 0 while the writer is still
        retrying, or once the re-anchor landed."""
        unlanded = self._unlanded_reanchor
        if unlanded is None or not self._failed:
            return 0
        return unlanded[0]

    def reanchor_lag_seconds(self, position: float | None = None) -> float:
        """How far re-anchors have put the audio behind `position_seconds()`
        since the last arm() or splice. Each one moves every later sample that
        much past the slot it was pushed for, so the sample heard now is the
        one pushed for ``position - reanchor_lag_seconds(position)``.

        ``position`` is the `position_seconds()` the caller subtracts this
        from, and the lag is taken at the read head it stands for; omitted,
        the head is read here. Read apart, the head moved between the two,
        and inside a hold (below) the heard sample stepped back by as much.

        The latest one's shift comes in only as the read head crosses the
        span it skipped, so the heard sample holds until the moved audio
        reaches it: audio already in the ring before the moved slot plays on
        time, and nothing current is heard from there to the anchor. Taken in
        whole at the re-anchor, the heard sample stepped back by the shift,
        and an analyzer reading it replayed audio it had already read, which
        was dropped late and never heard.

        Unlocked: the writer holds _io_lock across ring writes. The lag is
        one tuple the writer replaces whole, read where no re-anchor is in
        flight (_lag_seq even and unchanged across the read), so the head
        and the lag cannot straddle one: a lag published after the head was
        read was worked out at a later head, and taken at the earlier one it
        stepped the heard sample back, so it is taken at its own head; and a
        lag still in flight is waited out (there is no I/O inside it) rather
        than read stale against a head past the writer's.

        A re-anchor the writer gave up on comes off again (`_failed_lag_bytes`),
        as in `content_lag_seconds`, which is this lag with every hold crossed.

        0 on a stopped sampler, whose `position_seconds()` is 0: its lag was
        worked out at heads of a clock that has stopped, and taken at the
        last one it put the heard sample there, not at 0."""
        if not self._running:
            return 0.0
        if position is None:
            head = self._read_consumed_bytes()
        else:
            head = int(position * self._actual_rate) * self.bps
        while True:
            seq = self._lag_seq
            if seq & 1:
                time.sleep(0)  # yield to the writer finishing its re-anchor
                continue
            lag = self._reanchor_lag
            if self._lag_seq == seq:
                break
        at = max(head, lag[2])
        lag_bytes = self._lag_bytes(lag, at)
        failed = self._failed_lag_bytes()
        if failed:
            # The given-up shift's own hold is part of what the head has not
            # crossed, which _lag_bytes already left out: taking the whole
            # shift off on top took it twice, and inside that hold read short
            # of the lag that did land. Without the given-up re-anchors the
            # lag is the content lag, less any of the landed holds' rest the
            # head has not crossed, so it is the smaller of the two. Taken at
            # the lag's own head like the rest of it: worked out after the
            # step back below, an earlier position's heard sample fell behind
            # the one heard at that head.
            lag_bytes = min(lag_bytes, lag[0] - failed)
        lag_bytes -= at - head
        return lag_bytes / self.bps / self._actual_rate

    def _end_lag_window(self) -> None:
        """Under _io_lock: close the span _write_payload opened at its head
        read (_lag_seq back to even), once whatever lag it decided is in."""
        if self._lag_seq & 1:
            self._lag_seq += 1

    def _reanchor_lag_bytes(self, head: int) -> int:
        """The current re-anchor lag in bytes with the read head at ``head``."""
        return self._lag_bytes(self._reanchor_lag, head)

    @staticmethod
    def _lag_bytes(lag: tuple[int, tuple[tuple[int, int], ...], int], head: int) -> int:
        """``lag``'s bytes with the read head at ``head``: every shift, less
        what of each hold the head has not crossed yet."""
        total, holds, _ = lag
        return total - sum(max(0, to - max(head, frm)) for frm, to in holds)

    def _lag_after_reanchor(
        self, consumed: int, c: int, shift: int
    ) -> tuple[int, tuple[tuple[int, int], ...], int]:
        """Under _io_lock: the lag once the audio due at ``c`` is moved
        ``shift`` later, with the read head at ``consumed``.

        The heard sample holds at the later of the next unwritten one and the
        one already reported heard (no further than what was pushed, where an
        analysis tap's read stops), until the moved audio passes it. Held at
        the unwritten one alone, a re-anchor with the head past ``c`` stepped
        back over audio a tap had already handed the analyzer. A hold the head
        has not finished crossing is kept rather than taken in whole, since a
        sticky re-anchor can land up to a flush margin before the last one's
        anchor."""
        total, holds, _ = self._reanchor_lag
        heard = consumed - self._reanchor_lag_bytes(consumed)
        held = max(c - total, min(heard, self._pushed_samples * self.bps))
        start = max(c, consumed)
        # An older hold ends where this one starts: `held` already counts the
        # rest of it, and the two overlapping took the head's progress off
        # twice, so the heard sample jumped ahead and then ran backward.
        kept = tuple((frm, min(to, start)) for frm, to in holds if min(to, start) > consumed)
        return total + shift, (*kept, (start, held + total + shift)), consumed

    def ring_lead_seconds(self) -> float:
        """The ``AudioStreamer`` splice hook: how long after a flush() the first
        post-splice sample is heard. flush() keeps FLUSH_GUARD_S of old audio
        ahead of the read head and anchors the new audio right behind it, so a
        transport splice anchors the picture there too."""
        return self._flush_margin / self.bps / self._actual_rate

    def splice_position_seconds(self) -> float:
        """Where a sample fed now is heard: the read head plus
        `ring_lead_seconds()`, the margin a flush keeps ahead of it. The
        ``AudioStreamer`` splice hook, which reads its own clock once for
        the same sum."""
        return self.position_seconds() + self.ring_lead_seconds()

    def start_for_external_source(self, *, hold: bool = False) -> None:
        """Alias for ``start()`` so a caller feeding via ``push_samples`` (e.g.
        AudioFileSource) can bring up either backend with the same call. The DAC
        streamer uses this name for its no-input-thread bring-up; the sampler's
        ``start()`` already is that path (prefill + gate + writer thread)."""
        self.start(hold=hold)

    def _push_to_analysis(self, mono_floats: np.ndarray) -> None:
        """Feed the pre-DSP analysis sink, if one is installed. A failing analyzer
        must never take the audio path down, so the first exception is logged and
        the sink dropped for the rest of the run (mirrors
        AudioStreamer._push_to_analysis)."""
        sink = self.analysis_sink
        if sink is None:
            return
        try:
            sink(mono_floats)
        except Exception:
            if not self._analysis_sink_failed:
                self._analysis_sink_failed = True
                log.exception("sampler analysis sink failed — disabling it (playback continues)")
            self.analysis_sink = None

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

    def stop(self) -> None:
        """Gate the channel off and join the writer thread. Firmware-config
        restore (Audio Mixer / I/O map) is separate, in doctor at teardown.

        A writer that outlives the bounded join (wedged in a REU write on a
        stalled link) stays referenced, so the next arm()/start() refuses
        rather than run a second writer beside it, unless it had given up on
        the link (see `arm`). With `_running` cleared,
        it writes nothing more once that write returns."""
        self._stopped = True
        self._running = False
        self._held = False
        if self._writer is not None:
            self._writer.stop()
            if not self._writer.is_running():
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
        if self._late_bytes:
            # INFO: every splice drops the post-seek audio the demuxer delivers
            # after its slot, so this is routine; the underrun warning above is
            # the stall signal.
            log.info(
                "sampler: dropped %.2f s of audio that reached the ring after its slot",
                self._late_bytes / self.bps / self._actual_rate,
            )
        if self._reanchors > 1:
            log.warning(
                "sampler: re-anchored late audio %d times this session (producer "
                "slower than real time)",
                self._reanchors,
            )
        if self._restarts > 1:
            log.warning(
                "sampler: restarted the channel at its deadline %d times this session",
                self._restarts,
            )
        if self._lead_min is not None:
            log.info(
                "sampler: write-ahead lead min=%d max=%d bytes (target=%d, ring=%d)",
                self._lead_min,
                self._lead_max,
                self._lead_target,
                self.ring_size,
            )
        # Reported once per activation, however often stop() is called.
        self._underrun_pads = 0
        self._late_bytes = 0
        self._reanchors = 0
        self._restarts = 0
        self._lead_min = None
        self._lead_max = None
