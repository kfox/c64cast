"""The 6502 machine-code layer for NMI-driven $D418 DAC audio.

Pure data + pure functions only: the handler byte arrays audio.AudioStreamer
uploads to C64 RAM (the $C020 NMI DAC routine, the $C100 REU pump IRQ
handlers, the $C180 pump-body subroutine that modes_irq.py's chunked bank-swap
dispatcher JSRs into), the ring/pump memory-map constants those bytes are
assembled against, the control-loop tuning constants, and the pure pacing
helpers (stomp_spans, servo_period, nmi_rate_step) that keep the control
math unit-testable without hardware. Nothing here touches hardware or holds
state — bring-up, the worker thread, and teardown live in audio.AudioStreamer.

See docs/architecture/audio.md#audio_handlerspy--the-6502-machine-code-layer,
#why-the-ring-lives-at-4000, and #audiopy--audiostreamer (why not PWM).
"""

from __future__ import annotations

import numpy as np

from c64cast.hw.c64 import REU

# $D418 DAC NMI routine assembled at $C020 (32 bytes).
# Saves/restores only A; X and Y are untouched.
#
# Disassembly (NTSC NMI period = 127 cycles, fast path = 41 cycles total).
# Three HI bytes are patched at upload time from RING_BUFFER_HI /
# RING_BUFFER_END_HI so a future ring relocation is a one-line change:
#   $C020: 48           PHA                  ; save A
#   $C021: AD 0D DD     LDA $DD0D            ; ack CIA #2 NMI immediately
#   $C024: AD 00 ??     LDA $????            ; read sample (HI ← RING_BUFFER_HI)
#   $C027: 8D 18 D4     STA $D418            ; write to SID master volume
#   $C02A: EE 25 C0     INC $C025            ; advance read-pointer LO
#   $C02D: D0 0F        BNE $C03E            ; skip HI bump if no wrap
#   $C02F: EE 26 C0     INC $C026            ; advance read-pointer HI
#   $C032: AD 26 C0     LDA $C026            ; load HI for end-of-ring check
#   $C035: C9 ??        CMP #$??             ; end HI ← RING_BUFFER_END_HI
#   $C037: D0 05        BNE $C03E            ; not at end → done
#   $C039: A9 ??        LDA #$??             ; reset value ← RING_BUFFER_HI
#   $C03B: 8D 26 C0     STA $C026            ; restore pointer HI
#   $C03E: 68           PLA                  ; restore A
#   $C03F: 40           RTI
#
# With a badline (40 stolen cycles): handler takes 81 cycles total — well
# within the 127-cycle NTSC NMI period, so no NMI stacking occurs.
NMI_ROUTINE = bytes.fromhex(
    "48"  # PHA
    "AD0DDD"  # LDA $DD0D      ; ack NMI
    "AD0000"  # LDA $00??      ; read sample (HI patched at offset 6)
    "8D18D4"  # STA $D418      ; write to volume register
    "EE25C0"  # INC $C025      ; advance pointer LO
    "D00F"  # BNE +15        ; → $C03E (done)
    "EE26C0"  # INC $C026      ; advance pointer HI
    "AD26C0"  # LDA $C026      ; load HI for wrap check
    "C900"  # CMP #$??       ; wrap-end HI (patched at offset 22)
    "D005"  # BNE +5         ; → $C03E (done)
    "A900"  # LDA #$??       ; reset HI = RING_BUFFER_HI (patched at offset 26)
    "8D26C0"  # STA $C026      ; restore pointer HI
    "68"  # PLA
    "40"  # RTI
)
NMI_ROUTINE_PATCH_OFFSET_READ_HI = 6
NMI_ROUTINE_PATCH_OFFSET_WRAP_HI = 22
NMI_ROUTINE_PATCH_OFFSET_RESET_HI = 26
# Where the NMI routine lives in C64 RAM (these handlers "own" $C000-$C04F).
NMI_ROUTINE_ADDR = 0xC020

# 8 KB at $4000-$5FFF (VIC bank 1, which c64cast never selects — see
# audio.md#why-the-ring-lives-at-4000). The size is the paced worker's slack
# against latency spikes before the NMI read pointer laps the write pointer
# and starts replaying stale audio.
RING_BUFFER_ADDR = 0x4000
RING_BUFFER_SIZE = 0x2000
RING_BUFFER_END = RING_BUFFER_ADDR + RING_BUFFER_SIZE
RING_BUFFER_HI = RING_BUFFER_ADDR >> 8
RING_BUFFER_END_HI = RING_BUFFER_END >> 8

# A transport pause NEUTRAL-fills the unplayed span [R + guard, W). The guard
# leaves a stale tail un-stomped so the fill never races the NMI read head into
# a byte it is about to consume: 128 B ≈ 11 ms at the default rate.
STOMP_GUARD_BYTES = 128


def stomp_spans(
    r_addr: int, write_addr: int, guard: int = STOMP_GUARD_BYTES
) -> list[tuple[int, int]]:
    """Absolute-address DMA write spans covering the unplayed ring region
    ``(R + guard .. W)`` for the pause NEUTRAL-fill, splitting at
    ``RING_BUFFER_END`` when the region wraps. Returns ``[]`` when the gap is
    ``<= guard`` (nothing worth silencing past the tail we deliberately leave).

    Pure (no hardware); unit-tested. ``r_addr``/``write_addr`` are absolute
    ring addresses in ``[RING_BUFFER_ADDR, RING_BUFFER_END)``; ``write_addr`` is
    the worker's live W head (already advanced past the last byte written)."""
    gap = (write_addr - r_addr) % RING_BUFFER_SIZE
    if gap <= guard:
        return []
    start = RING_BUFFER_ADDR + ((r_addr - RING_BUFFER_ADDR + guard) % RING_BUFFER_SIZE)
    length = gap - guard
    spans: list[tuple[int, int]] = []
    first = min(length, RING_BUFFER_END - start)
    spans.append((start, first))
    if first < length:
        spans.append((RING_BUFFER_ADDR, length - first))
    return spans


NEUTRAL_SAMPLE = 7  # mid-scale 4-bit value; keeps the speaker cone centered

# CIA #2 control words for NMI bring-up / teardown. Each pair is one
# write_regs(CIA2.ICR, ...) = $DD0D then $DD0E, so the second byte of every pair
# lands in CRA, not in ICR:
#  - DISABLE: clear all five IRQ-source bits in ICR (high bit = 0 → clear).
#  - CRA_STOP: the CRA companion — Timer A stopped. This does NOT clear the
#    latched ICR *flags*; only a read of $DD0D does (NmiTimer.arm_once).
#  - ENABLE_TIMER_A_NMI: set bit 7 + bit 0 (enable timer-A IRQ source).
#  - TIMER_A_CONTINUOUS: continuous mode, start (CRA bits 0+4).
CIA2_ICR_DISABLE_ALL = 0x7F
CIA2_CRA_STOP = 0x00
CIA2_ICR_ENABLE_TIMER_A_NMI = 0x81
CIA2_TIMER_A_CONTINUOUS = 0x11

# The arm is verified by watching R move, because the CIA #2 writes ride a
# transport whose _emit absorbs a failed write instead of raising. 30 ms is
# unambiguous: R advances at ≈sample_rate B/s, so ≈240 bytes at 8 kHz. Five
# attempts cost ≈150 ms in the failure case only, against a ≈768 ms prebuffer.
NMI_ARM_MAX_ATTEMPTS = 5
NMI_ARM_VERIFY_DELAY_S = 0.03
# Consecutive identical R readings before the servo warns that the consumer
# died mid-session. R moves ~1 KB per chunk period when alive.
NMI_STALL_WARN_CHUNKS = 8

# Share of the backend's sustained write-rate ceiling the audio drip may spend.
# The render thread wants the rest of the same socket, and audio that overruns
# its slots stops collecting and pads silence (AudioStreamer._halt_quantum).
AUDIO_WRITE_RATE_SHARE = 0.5

# Float-sample → 4-bit volume code: (x + 1) * VOLUME_SCALE, clipped to
# [0, MAX_VOLUME]. Centers a [-1, 1] input on 7.5 → DAC ~half-scale.
DAC_VOLUME_SCALE = 7.5
DAC_MAX_VOLUME = 15
INT16_FULL_SCALE = 32768.0  # divisor to map int16 → float [-1, 1]
INT16_MAX = 32767  # int16 saturation bounds (np.iinfo(np.int16))
INT16_MIN = -32768

# Float-sample → 8-bit amplitude index for the Mahoney companding path:
# 128 + x * AMP_SCALE, clipped to [0, 255]. Centers a [-1, 1] input on 128
# (silence). The index then looks up the curve's $D418 byte via sidtable[idx].
DAC_AMP_CENTER = 128
DAC_AMP_SCALE = 128.0
DAC_AMP_MAX = 255


def encode_floats_to_dac(
    floats: np.ndarray,
    *,
    dither: bool,
    rng: np.random.Generator | None = None,
    curve: np.ndarray | None = None,
) -> np.ndarray:
    """Quantize float audio samples in [-1, 1] to SID ``$D418`` DAC bytes, as a
    uint8 array. Single source of truth for the DAC encoding shared by every
    input path (host-DMA mic, REU mic, offline video pre-encode) — the
    quantization math must stay identical across them or REU-mode and host-mode
    levels would silently diverge.

    curve: when ``None`` (default) the samples are quantized to the 4-bit SID
    volume nibble (0..15) — the legacy linear path, bit-identical to before.
    When a 256-entry amplitude→``$D418`` table is passed (see
    ``dac_curves.resolve_dac_curve``), samples are mapped to an 8-bit amplitude
    index centered on 128 and looked up in the table, yielding the Mahoney
    full-byte codes (0..255, ~6-7 effective bits). The caller must have written
    the Mahoney SID env for those bytes to decode correctly.

    TPDF dither (``dither=True``): a triangular ±1 LSB random offset added
    pre-quantization, decorrelating the rounding error from the signal. At
    4 bits the coarse rounding otherwise produces signal-correlated harmonic
    distortion (buzz/chop on speech); dither turns the same total error into
    smooth white-noise hiss, perceptually less intrusive. The triangular shape
    comes from subtracting two independent uniform [0, 1) draws (Wannamaker
    1992). Exact-zero input samples skip dither so gated silence stays silent
    (the mic + AVFileSource noise gates zero the noise floor — adding hiss
    there would repaint what they cleared). For the companding path the same
    dither is folded in the amplitude-index domain (±1 index step).

    rng: the generator the dither is drawn from. Required whenever
    ``dither`` is True — a ``ValueError`` otherwise, rather than a silent
    fall back to a process-wide sequence no caller can reproduce or own.
    Ignored when ``dither`` is False."""
    if curve is None:
        scale = DAC_VOLUME_SCALE
        code_float = (floats + 1.0) * scale
        code_max = DAC_MAX_VOLUME
    else:
        code_float = DAC_AMP_CENTER + floats * DAC_AMP_SCALE
        code_max = DAC_AMP_MAX
    if dither:
        if rng is None:
            raise ValueError("encode_floats_to_dac: dither=True needs an rng")
        d = rng.random(floats.shape, dtype=np.float32) - rng.random(floats.shape, dtype=np.float32)
        d[floats == 0] = 0.0
        code_float = code_float + d
    idx = np.clip(code_float, 0, code_max).astype(np.uint8)
    if curve is None:
        return idx
    # Amplitude index → measured $D418 byte. curve is uint8[256]; fancy-index
    # is bounds-safe because idx was clipped to [0, 255].
    return curve[idx]


# One ring write's worth of samples, and the unit the worker's pace schedule is
# quantized to (chunk_period = CHUNK_SIZE / effective_rate ≈ 85 ms at 12 kHz).
# Must stay an exact divisor of RING_BUFFER_SIZE: that is what keeps write_addr
# on a grid the ring end falls on (the NEUTRAL tail pad in AudioStreamer._worker).
CHUNK_SIZE = 1024
AUDIO_QUEUE_MAX_BLOBS = 256  # outer cap (per-blob, not per-sample)
MAX_QUEUED_SAMPLES = 16384  # soft cap (~1.4 s @ 12 kHz)
PREBUFFER_CHUNKS = 6  # chunks to buffer before starting NMI
QUEUE_PUT_TIMEOUT_S = 0.2
BACKPRESSURE_SPIN_S = 0.005  # sleep between full-queue retries
# How long stop() waits for the worker to leave its loop. A chunk period is
# ~85 ms at the default; what this bounds is a ring write on a stalled link.
WORKER_JOIN_TIMEOUT_S = 1.0

# Pre-quantization float samples in [-1, 1] for FFT-based overlays. ~170 ms at
# 12 kHz, giving a usable FFT down to ~45 Hz.
SAMPLE_TAP_SIZE = 2048


# REU-staged audio pump ([audio].use_reu_pump). The whole track is preloaded
# into REU SDRAM (socket DMA opcode 0xFF07 REUWRITE), and a 6502 IRQ handler at
# $C100 triggers REU→ring DMAs at the CIA #1 rate to refill the ring at $4000,
# replacing the host-side DMAWRITEs that audibly perturb SID output.
# See docs/architecture/audio.md#audiouse_reu_pump--reu-staged-mic-streaming.

REU_PUMP_HANDLER_ADDR = 0xC100  # IRQ handler lives here; $C020 NMI handler stays
REU_AUDIO_BASE = 0x000000  # REU offset where preloaded audio starts
REU_PUMP_CHUNK_SIZE = 128  # bytes per IRQ-triggered REU DMA (default)
REU_UPLOAD_SLICE = 32 * 1024  # bytes per socket REUWRITE (one per slice)

# First REU offset the staged-audio upload must NOT reach: the video staging
# area the bank-swap bitmap path owns (video/modes_irq.REU_VIDEO_SCREEN_BASE),
# which is the only neighbor live in the same scene. Kept as a local number so
# the audio layer takes no dependency on the video layer;
# tests/test_reu_audio.py asserts the two against each other.
REU_AUDIO_REGION_END = 0xE00000
REU_AUDIO_MAX_BYTES = REU_AUDIO_REGION_END - REU_AUDIO_BASE

# Write-behind-read margin for the pump's initial pointer placement.
#
# W (pump) and R (NMI reader) walk the ring at the same average rate, so REU
# sample N always lands at ring position (N mod RING_BUFFER_SIZE) and what
# matters is the *pointer gap*. Crossing it either way is the same audible
# overlap: R catching W reads a lap-old span, W catching R writes next-lap
# samples over the current one. Both happen here, because a bus halt starves
# either the NMI ticks or the CIA #1 pump IRQ depending on which IRQ source
# the halt window collapses.
#
# Seeding W exactly half a ring behind R (dst = src = RING_BUFFER_SIZE/2) is
# the symmetric optimum: half a ring of jitter headroom in both directions.
# Continuity is unaffected because src offset ≡ dst position (mod ring) — the
# pump re-writes the upper half of the prefill with identical bytes once at
# startup. See AudioStreamer.start_for_reu_staged step 3.
REU_PUMP_INITIAL_MARGIN = RING_BUFFER_SIZE // 2  # 4096 B = half the ring

# A heavy-bus display mode (mhires DMAWRITE at ~300 KB/s) costs NMI ~30% of
# its ticks — HW-measured 4020 Hz effective, U64, 2026-05-26 — so the default
# chunk of 128 over-produces 2x and overflows the ring in ~2 s. 80 slightly
# under-produces in the worst case (NEUTRAL padding on a few percent of
# samples, mild hiss) and never overflows. Char/blank scenes keep 128.
REU_PUMP_CHUNK_SIZE_HEAVY_BUS = 80

# The matched pump latch AT 8 kHz ONLY — a reference value, never a default to
# write. Pump period = chunk × NMI period, so chunk 128 at an NMI latch of 127
# gives 16384 cyc and a latch of $3FFF. The ratio is system-independent but NOT
# rate-independent: at the 12 kHz default the matched latch is 128 × 85 - 1 =
# 10879, so writing this would under-produce by 85/128. Both pump paths derive
# it live instead (AudioStreamer._program_reu_pump_rate).
REU_PUMP_CIA1_LATCH_8KHZ = 0x3FFF

# A CIA Timer A latch is two 8-bit registers, so a derived latch above this
# is silently truncated modulo 65536 by the register write.
CIA_TIMER_LATCH_MAX = 0xFFFF

# Between arming the NMI consumer and arming the C64-side pump, so the NMI is
# already firing when the first pump DMA lands — otherwise that DMA overwrites
# ring positions the NMI has not read yet. Both pump bring-ups wait it out.
REU_PUMP_SETTLE_S = 0.05

# C64-side REU-pump rate governor. The pump produces at the fixed nominal CIA
# #1 rate while video DMA bus-halts throttle the NMI reader below it, so the
# write head laps the reader every ~15-23 s (echo; scripts/diags/
# reu_margin_probe.py). It regulates entirely on the C64, with zero host bus
# writes during playback: the pump's own IRQ reads R and skips its chunk when
# the write head is too far ahead. The nominal rate is always >= the (only ever
# throttled) consumer, so skip-when-ahead caps the gap and never underruns.
#
# Gap is in 256-byte (HI-byte) units. The ring spans 32 HI values ($40-$5F), so
# gap_hi = (dst_hi - R_hi) & $1F. Masking to 5 bits also discards the garbage
# the U64 REU returns in the upper bits of the dst HI read-back. The threshold
# is half a ring, matching the bring-up seed, so bang-bang control parks the gap
# symmetrically with ~4 KB before either a lap or an underrun.
REU_GOVERNOR_GAP_THRESHOLD_HI = REU_PUMP_INITIAL_MARGIN >> 8  # 16 (= half ring)
# NMI read pointer HI byte (R_hi): NMI_ROUTINE self-modifying operand at
# $C026. The plain governor reads this directly on-chip; the host never writes.
READ_PTR_HI_ADDR = NMI_ROUTINE_ADDR + 6  # $C026

# Host-DMA pacing servo (closed-loop W→R rate match). The worker paces ring
# writes strictly to wall-clock, so W advances at exactly sample_rate B/s, while
# R loses ~4% of its ticks to video DMA bus-halts (HW-measured ~7690 B/s against
# an 8000 B/s producer) — W laps the 8 KB ring every ~26 s. Since W is paced by
# time.sleep, the loop closes with zero C64 writes: the worker reads R once per
# chunk and a PI controller stretches or shrinks the per-chunk pace so the ring
# gap locks near half a ring. See scripts/diags/hostdma_drift_probe.py.
READ_PTR_LO_ADDR = NMI_ROUTINE_ADDR + 5  # $C025 (R operand low byte)
HOST_DMA_SERVO_TARGET_GAP = RING_BUFFER_SIZE // 2  # 4096 B (half ring)
# HW-empirical gains. The drift to cancel is ~310 B/s, i.e. a steady period
# stretch of ~+5 ms/chunk. KP = 5e-6 s/byte makes a 1000-byte phase error add
# +5 ms (recovers in ~1-2 s); KI, an order below, nulls the fixed offset
# proportional control alone would leave.
HOST_DMA_SERVO_KP = 5e-6  # s/byte            (HW-TUNABLE)
HOST_DMA_SERVO_KI = 5e-7  # s/(byte*chunk)    (HW-TUNABLE)
HOST_DMA_SERVO_INTEG_CLAMP = 0.5  # max |ki*integ|, frac of chunk_period
HOST_DMA_SERVO_PERIOD_MIN_FRAC = 0.5
HOST_DMA_SERVO_PERIOD_MAX_FRAC = 1.5

# Seconds between the worker's health lines (0 disables). Short enough to place
# an onset within a few seconds of where a listener hears it, long enough that
# a normal -v run is not drowned by it.
AUDIO_HEALTH_LOG_INTERVAL_S = 5.0

# Adaptive NMI-rate compensation: a slow outer loop that shrinks the CIA #2
# Timer A latch until the *measured* R rate lands back at sample_rate, since the
# gap servo above otherwise locks playback to the bus-halt-throttled consumer.
# It servos on dR/dt, not the gap — the gap servo nulls the gap, so it carries
# no rate signal and the two loops would fight. Clamped to the handler cycle
# budget (c64.NMI_SAFE_MIN_PERIOD_CYCLES). Off by default; see
# docs/architecture/audio.md#host-dma-pitch-compensation--why-two-of-the-three-knobs-default-off.
#
# The deadband MUST stay >= one latch quantum (~1% rate/step): the latch is an
# integer, so a narrower one limit-cycles ±1 step, an audible ~1% pitch wobble.
# The EMA alpha sets the estimator time constant (~chunk_period/alpha ≈ 2.1 s at
# 12 kHz / 1024-byte chunks) — long enough to reject torn-16-bit-read noise,
# short enough to re-acquire after a scene cut. The coarse zone converges a cold
# start in ~2-3 s instead of ~9 s; the fine zone moves ±1 so steady-state pitch
# steps are inaudible.
NMI_RATE_LOOP_DEADBAND_FRAC = 0.013  # > one latch step (~1%); avoids limit cycle
NMI_RATE_LOOP_COARSE_ZONE_FRAC = 0.03  # above this error, take a proportional step
NMI_RATE_LOOP_MAX_COARSE_STEP = 4  # cap acquisition step (latch units)
NMI_RATE_LOOP_EMA_ALPHA = 0.04  # per-chunk EMA weight for the R-rate estimate (fine)
# Acquisition phase: a more responsive EMA and a short decision cadence walk the
# latch to convergence in ~0.5 s, so the start-of-playback pitch glide is brief
# instead of a ~3 s audible rise. The first decision needing no change flips to
# the fine loop above.
NMI_RATE_LOOP_ACQUIRE_ALPHA = 0.4  # responsive EMA during acquisition
NMI_RATE_LOOP_ACQUIRE_DECIDE_CHUNKS = 2  # decide every ~2 chunks while acquiring
# Warm-up gate: hold the latch at the near-converged seed and suppress decisions
# for this long after a consumer start or a large playback disturbance, while
# still feeding R into the EMA. The first R samples after a start/seek are
# unrepresentative — post-seek decode catch-up and the playlist's frame-drop snap
# mean R reads high until the steady-state VIC/DMA tick loss arrives (~3 s on HW:
# R ≈ 11.5k → 10.1k). Re-armed by AudioStreamer.note_playback_disturbance().
NMI_RATE_LOOP_WARMUP_S = 3.0
# Per-mode-class seed for the loop's starting latch, so playback begins near the
# converged rate. Bitmap modes lose ~10% of NMI ticks to the REU bank-swap +
# badline DMA and converge near the ceiling; char/light modes near nominal.
# Refined per mode by NmiTimer.learned_latch as scenes converge.
NMI_BITMAP_SEED_MODES = frozenset({"hires", "mhires"})

# REC command byte for REU DMA: bit 7 = exec, bit 4 = FF00 disable (execute
# immediately, no $FF00 trigger needed), bits 1:0 = 01 = REU → C64 fetch.
# Autoload bit (5) is OFF so the source address auto-increments across triggers.
# Aliased from c64.REU.CMD_FETCH_EXEC so the byte arrays below read with a
# local name.
REU_CMD_FETCH_EXEC = REU.CMD_FETCH_EXEC  # $91


def _assert_chunk_offsets(handler: bytes, offsets: tuple[int, ...], name: str) -> None:
    """Check that every ``*_CHUNK_OFFSETS`` entry really addresses a
    chunk-length operand in ``handler``, LO first then HI, alternating.

    The offsets are what ``AudioStreamer.start_for_reu_staged`` patches a
    per-scene chunk size into, and a wrong one writes a length into some
    other instruction's operand — which DMAs from or to a garbage address
    (bursts of static into the ring, writes into color RAM). The length
    asserts beside each handler catch a size change; this catches a
    same-length re-arrangement, which they cannot see.
    """
    lo = REU_PUMP_CHUNK_SIZE & 0xFF
    hi = (REU_PUMP_CHUNK_SIZE >> 8) & 0xFF
    for i, off in enumerate(offsets):
        expected = lo if i % 2 == 0 else hi
        assert handler[off] == expected, (
            f"{name} offset {off} holds ${handler[off]:02X}, not the "
            f"chunk-size {'LO' if i % 2 == 0 else 'HI'} byte ${expected:02X} — "
            "the handler bytes were re-assembled without moving the offsets."
        )


def patch_chunk_size(handler: bytes, offsets: tuple[int, ...], chunk: int) -> bytes:
    """Return ``handler`` with a per-scene ``chunk`` size written into the
    length operands at ``offsets`` (LO then HI, alternating — the shape of
    every ``*_CHUNK_OFFSETS`` tuple in this module)."""
    patched = bytearray(handler)
    for i, off in enumerate(offsets):
        patched[off] = (chunk >> (0 if i % 2 == 0 else 8)) & 0xFF
    return bytes(patched)


# 6502 IRQ handler at $C100. PHA / re-set length (the U64's REU decrements
# the length register during transfer; without re-setting, subsequent triggers
# would transfer only 1 byte) / trigger DMA / wrap dest from RING_END → RING /
# PLA / JMP $EA31 (chain to kernal IRQ for keyboard scan + jiffy clock).
#
# Byte-level layout (offsets relative to $C100). The two HI bytes that pin
# the ring boundary (CMP #end_hi at offset 20, LDA #start_hi at offset 24)
# come from RING_BUFFER_END_HI / RING_BUFFER_HI so a future ring relocation
# is a one-line change to those constants:
#   0  PHA                       1 byte   ; save A
#   1  LDA #$80                  2 bytes  ┐ re-set length = REU_PUMP_CHUNK_SIZE
#   3  STA $DF07                 3 bytes  │  (U64 REU decrements during transfer;
#   6  LDA #$00                  2 bytes  │   without this, 2nd+ triggers do 1 byte)
#   8  STA $DF08                 3 bytes  ┘
#  11  LDA #$91                  2 bytes  ; REU exec + no autoload + REU→C64
#  13  STA $DF01                 3 bytes  ; trigger DMA (CPU halts ~128 cyc)
#  16  LDA $DF03                 3 bytes  ; read dest_hi after auto-inc
#  19  CMP #end_hi               2 bytes  ; one past ring end? (= RING_BUFFER_END_HI)
#  21  BCC +10 → PLA at offset 33  2 bytes
#  23  LDA #start_hi             2 bytes  ┐ wrap dest = RING_BUFFER_ADDR
#  25  STA $DF03                 3 bytes  │
#  28  LDA #$00                  2 bytes  │
#  30  STA $DF02                 3 bytes  ┘
#  33  PLA                       1 byte   ← BCC target
#  34  JMP $EA31                 3 bytes  ; chain to kernal IRQ
#
# Total = 37 bytes. The BCC offset MUST be exactly +10 to reach PLA at offset
# 33 — a +8 displacement lands on the `$02` of STA $DF02, opcode $02, and the
# CPU JAMs. The assertion below catches length mismatches only; if you edit the
# bytes, verify the branch targets by hand.
REU_IRQ_HANDLER = bytes(
    [
        0x48,  # PHA
        0xA9,
        REU_PUMP_CHUNK_SIZE & 0xFF,  # LDA #<chunk_size
        0x8D,
        0x07,
        0xDF,  # STA $DF07
        0xA9,
        (REU_PUMP_CHUNK_SIZE >> 8) & 0xFF,  # LDA #>chunk_size
        0x8D,
        0x08,
        0xDF,  # STA $DF08
        0xA9,
        REU_CMD_FETCH_EXEC,  # LDA #$91
        0x8D,
        0x01,
        0xDF,  # STA $DF01
        0xAD,
        0x03,
        0xDF,  # LDA $DF03
        0xC9,
        RING_BUFFER_END_HI,  # CMP #end_hi
        0x90,
        0x0A,  # BCC +10 → PLA at offset 33
        0xA9,
        RING_BUFFER_HI,  # LDA #start_hi
        0x8D,
        0x03,
        0xDF,  # STA $DF03
        0xA9,
        0x00,  # LDA #$00
        0x8D,
        0x02,
        0xDF,  # STA $DF02
        0x68,  # PLA
        0x4C,
        0x31,
        0xEA,  # JMP $EA31
    ]
)
assert len(REU_IRQ_HANDLER) == 37, (
    "REU_IRQ_HANDLER length changed — BCC offset (currently +10) may need "
    "to be recomputed to reach the PLA byte after the wrap block."
)
# Where a per-scene chunk size is patched in: LDA #<chunk operand at 2,
# LDA #>chunk at 7.
REU_IRQ_HANDLER_CHUNK_OFFSETS = (2, 7)
_assert_chunk_offsets(REU_IRQ_HANDLER, REU_IRQ_HANDLER_CHUNK_OFFSETS, "REU_IRQ_HANDLER")


# REU_IRQ_HANDLER + an 18-byte governor prefix. Before pumping, read the
# write head (dst HI, $DF03) and the NMI read pointer (R HI, $C026), compute
# the ring gap in 256-byte units, and if the write head is already >= half a
# ring ahead, SKIP this chunk (don't trigger, don't advance) so the reader
# catches up. Otherwise fall through to the unmodified pump body: the gap
# self-regulates near half a ring with no CIA reprogramming and no host writes.
#
# Byte layout (offsets relative to $C100):
#   0   PHA
#   1   LDA $DF03            ; dst_hi (write head, pre-trigger)
#   4   SEC
#   5   SBC $C026            ; - R_hi (NMI read pointer HI)
#   8   AND #$1F             ; gap_hi mod 32 (also masks REU read-back garbage)
#  10   CMP #threshold_hi    ; >= half ring ahead?
#  12   BCC +4 → offset 18   ; gap small → pump normally
#  14   PLA                  ; skip: too far ahead, let reader catch up
#  15   JMP $EA31            ; chain to kernal IRQ (keyboard/jiffy still serviced)
#  18   <REU_IRQ_HANDLER body without its leading PHA: pump + dst wrap + PLA + JMP>
#
# The skipped PLA balances the offset-0 PHA on both paths. The body's internal
# BCC (+10 to its PLA) is relative and unchanged by the prefix shift.
REU_IRQ_HANDLER_GOVERNOR_PREFIX_LEN = 18
REU_IRQ_HANDLER_GOVERNOR = (
    bytes(
        [
            0x48,  # PHA
            0xAD,
            0x03,
            0xDF,  # LDA $DF03   (dst_hi)
            0x38,  # SEC
            0xED,
            READ_PTR_HI_ADDR & 0xFF,
            (READ_PTR_HI_ADDR >> 8) & 0xFF,  # SBC $C026   (R_hi)
            0x29,
            0x1F,  # AND #$1F    (gap_hi)
            0xC9,
            REU_GOVERNOR_GAP_THRESHOLD_HI,  # CMP #threshold_hi
            0x90,
            0x04,  # BCC +4 → pump body (offset 18)
            0x68,  # PLA  (skip path)
            0x4C,
            0x31,
            0xEA,  # JMP $EA31
        ]
    )
    + REU_IRQ_HANDLER[1:]
)  # pump body, sans leading PHA
assert len(REU_IRQ_HANDLER_GOVERNOR) == REU_IRQ_HANDLER_GOVERNOR_PREFIX_LEN + 36, (
    "REU_IRQ_HANDLER_GOVERNOR length changed — the governor prefix is 18 bytes "
    "(BCC +4 over the 4-byte skip block) followed by REU_IRQ_HANDLER[1:]."
)
# Pump body must start exactly at offset 18 (the BCC +4 target).
assert REU_IRQ_HANDLER_GOVERNOR[REU_IRQ_HANDLER_GOVERNOR_PREFIX_LEN] == REU_IRQ_HANDLER[1], (
    "governor pump-body offset drifted from the BCC +4 target (18)"
)
# The body is REU_IRQ_HANDLER[1:] behind the prefix, so every plain offset
# shifts by (prefix - 1): 2 → 19, 7 → 24. Derived rather than re-typed.
REU_IRQ_HANDLER_GOVERNOR_CHUNK_OFFSETS = tuple(
    off + REU_IRQ_HANDLER_GOVERNOR_PREFIX_LEN - 1 for off in REU_IRQ_HANDLER_CHUNK_OFFSETS
)
_assert_chunk_offsets(
    REU_IRQ_HANDLER_GOVERNOR,
    REU_IRQ_HANDLER_GOVERNOR_CHUNK_OFFSETS,
    "REU_IRQ_HANDLER_GOVERNOR",
)


# $C200 slot, just past the audio handler region ($C100-$C1FF). Both the mic
# pump and the tracked video pump load $DF04/$DF05/$DF06 from this 3-byte
# tracker every IRQ; a scene runs at most one of them, so sharing is safe.
REU_AUDIO_SRC_TRACKER_ADDR = 0xC200
_TRK_LO = REU_AUDIO_SRC_TRACKER_ADDR & 0xFF
_TRK_HI_BYTE = (REU_AUDIO_SRC_TRACKER_ADDR >> 8) & 0xFF

# Same pattern as api.py SID_PLAYER_MC_TEMPLATE: the handler DECs a counter and
# chains to the full kernal IRQ tail ($EA31: SCNKEY + UDTIM + cursor blink) only
# every Nth tick; the other N-1 take a lean exit (LDA $DC0D / JMP $EA81 — ack
# CIA #1, restore, RTI). In mhires the cursor-blink cell at $0400+cursor_pos is
# a *color attribute*, so each blink flips a cell's color — dividing the rate
# divides the visible flicker. Counter byte at $C205, just past the 5-byte
# src/dst tracker at $C200-$C204.
REU_PUMP_TICK_COUNTER_ADDR = 0xC205
_TCTR_LO = REU_PUMP_TICK_COUNTER_ADDR & 0xFF
_TCTR_HI_BYTE = (REU_PUMP_TICK_COUNTER_ADDR >> 8) & 0xFF
# N=3 → kernal tail at ~33 Hz with chunk=80 (CIA #1 @ 100 Hz): below the 60 Hz
# the kernal expects, but no service depends on the exact rate and it still
# clears the 10 Hz keyboard poller. Do not exceed 8 — SCNKEY then misses held
# keys.
REU_PUMP_TICK_DIVIDER = 3


# The plain REU_IRQ_HANDLER above relies on REU source ($DF04-$DF06) AND
# C64 dest ($DF02-$DF03) auto-incrementing across triggers — works in
# isolation, FAILS when the REU bank-swap video pipeline ALSO uses the
# REC controller. After a raster IRQ triggers bitmap+screen+(color) DMAs,
# BOTH src and dst point into the video regions; the next audio IRQ
# would then read from video staging and write into color RAM (audible
# as sparse bursts / "thuds", visible as garbage on screen).
#
# Fix: read+write BOTH src AND dst from main-RAM trackers on every audio
# IRQ. The host seeds them at audio bring-up.
#   $C200-$C202  src LO/MI/HI (24-bit REU offset)
#   $C203-$C204  dst LO/HI    (16-bit main RAM addr inside the audio ring)
#
# Used INSTEAD OF REU_IRQ_HANDLER when AudioStreamer.start_for_reu_staged is
# called with skip_irq_vector_hook=True — the display mode's merged bank-swap
# dispatcher owns $0314 and reaches the audio handler via its JMP $C100
# fall-through. The solo audio path keeps the plain handler.
#
# Byte layout (offsets relative to $C100):
#   0    PHA
#   1    LDA #<chunk_size / STA $DF07              ┐ re-set length
#   6    LDA #>chunk_size / STA $DF08              ┘
#  11    LDA src_lo / STA $DF04                    ┐
#  17    LDA src_mi / STA $DF05                    │ load src from tracker
#  23    LDA src_hi / STA $DF06                    ┘
#  29    LDA dst_lo / STA $DF02                    ┐ load dst from tracker
#  35    LDA dst_hi / STA $DF03                    ┘
#  41    LDA #$91 / STA $DF01                      ; trigger DMA
#  46    CLC                                       ┐ advance src by chunk
#  47    LDA src_lo / ADC #<chunk / STA src_lo     │
#  55    LDA src_mi / ADC #>chunk / STA src_mi     │
#  63    LDA src_hi / ADC #$00 / STA src_hi        ┘
#  71    CLC                                       ┐ advance dst by chunk
#  72    LDA dst_lo / ADC #<chunk / STA dst_lo     │
#  80    LDA dst_hi / ADC #>chunk / STA dst_hi     ┘
#  88    LDA dst_hi / CMP #ring_end_hi             ; dst wrap check
#  93    BCC +10 → offset 105 (PLA)
#  95    LDA #ring_start_hi / STA dst_hi           ┐ wrap dst to ring start
# 100    LDA #$00 / STA dst_lo                     ┘
# 105    PLA                                       ; restore A (local PHA)
# 106    DEC counter ($C205)                       ┐ tick divider:
# 109    BNE +8 → offset 119 (lean exit)           │   chain every Nth
# 111    LDA #N / STA counter                      │   tick, lean-exit
# 116    JMP $EA31  (full kernal tail)             ┘   the other N-1.
# 119    LDA $DC0D                                 ┐ lean exit:
# 122    JMP $EA81                                 ┘   ack + RTI
#
# Total = 125 bytes. BCC at offset 93 with +10 lands on PLA at offset 105.
# Inner BNE at offset 109 with +8 lands on LDA $DC0D at offset 119.
# Chunk-size patch offsets: 2, 7, 51, 59, 76, 84.
# Divider N patch offset: 112 (the immediate byte after LDA #).
REU_IRQ_HANDLER_TRACKED = bytes(
    [
        0x48,  # PHA
        # re-set length (auto-decrements during DMA, must reload):
        0xA9,
        REU_PUMP_CHUNK_SIZE & 0xFF,  # LDA #<chunk_size
        0x8D,
        0x07,
        0xDF,  # STA $DF07
        0xA9,
        (REU_PUMP_CHUNK_SIZE >> 8) & 0xFF,  # LDA #>chunk_size
        0x8D,
        0x08,
        0xDF,  # STA $DF08
        # load src from main-RAM tracker (works around bank-swap stomping REC):
        0xAD,
        _TRK_LO,
        _TRK_HI_BYTE,  # LDA src_lo
        0x8D,
        0x04,
        0xDF,  # STA $DF04
        0xAD,
        (_TRK_LO + 1) & 0xFF,
        _TRK_HI_BYTE,  # LDA src_mi
        0x8D,
        0x05,
        0xDF,  # STA $DF05
        0xAD,
        (_TRK_LO + 2) & 0xFF,
        _TRK_HI_BYTE,  # LDA src_hi
        0x8D,
        0x06,
        0xDF,  # STA $DF06
        # load dst from main-RAM tracker (same rationale — bank-swap stomps these too):
        0xAD,
        (_TRK_LO + 3) & 0xFF,
        _TRK_HI_BYTE,  # LDA dst_lo
        0x8D,
        0x02,
        0xDF,  # STA $DF02
        0xAD,
        (_TRK_LO + 4) & 0xFF,
        _TRK_HI_BYTE,  # LDA dst_hi
        0x8D,
        0x03,
        0xDF,  # STA $DF03
        # trigger DMA:
        0xA9,
        REU_CMD_FETCH_EXEC,  # LDA #$91
        0x8D,
        0x01,
        0xDF,  # STA $DF01
        # advance src tracker by chunk_size:
        0x18,  # CLC
        0xAD,
        _TRK_LO,
        _TRK_HI_BYTE,  # LDA src_lo
        0x69,
        REU_PUMP_CHUNK_SIZE & 0xFF,  # ADC #<chunk_size
        0x8D,
        _TRK_LO,
        _TRK_HI_BYTE,  # STA src_lo
        0xAD,
        (_TRK_LO + 1) & 0xFF,
        _TRK_HI_BYTE,  # LDA src_mi
        0x69,
        (REU_PUMP_CHUNK_SIZE >> 8) & 0xFF,  # ADC #>chunk_size
        0x8D,
        (_TRK_LO + 1) & 0xFF,
        _TRK_HI_BYTE,  # STA src_mi
        0xAD,
        (_TRK_LO + 2) & 0xFF,
        _TRK_HI_BYTE,  # LDA src_hi
        0x69,
        0x00,  # ADC #$00 (carry only)
        0x8D,
        (_TRK_LO + 2) & 0xFF,
        _TRK_HI_BYTE,  # STA src_hi
        # advance dst tracker by chunk_size:
        0x18,  # CLC
        0xAD,
        (_TRK_LO + 3) & 0xFF,
        _TRK_HI_BYTE,  # LDA dst_lo
        0x69,
        REU_PUMP_CHUNK_SIZE & 0xFF,  # ADC #<chunk_size
        0x8D,
        (_TRK_LO + 3) & 0xFF,
        _TRK_HI_BYTE,  # STA dst_lo
        0xAD,
        (_TRK_LO + 4) & 0xFF,
        _TRK_HI_BYTE,  # LDA dst_hi
        0x69,
        (REU_PUMP_CHUNK_SIZE >> 8) & 0xFF,  # ADC #>chunk_size
        0x8D,
        (_TRK_LO + 4) & 0xFF,
        _TRK_HI_BYTE,  # STA dst_hi
        # dst wrap check on the tracker value (NOT $DF03 — that's now stale
        # whenever bank-swap ran between IRQs):
        0xAD,
        (_TRK_LO + 4) & 0xFF,
        _TRK_HI_BYTE,  # LDA dst_hi
        0xC9,
        RING_BUFFER_END_HI,  # CMP #ring_end_hi
        0x90,
        0x0A,  # BCC +10 → offset 105 (PLA)
        0xA9,
        RING_BUFFER_HI,  # LDA #ring_start_hi
        0x8D,
        (_TRK_LO + 4) & 0xFF,
        _TRK_HI_BYTE,  # STA dst_hi
        0xA9,
        0x00,  # LDA #$00
        0x8D,
        (_TRK_LO + 3) & 0xFF,
        _TRK_HI_BYTE,  # STA dst_lo
        # end:
        0x68,  # PLA (offset 105)
        # tick divider (offsets 106-124): chain to $EA31 every Nth tick, lean
        # exit the other N-1. Same shape as api.py's SID_PLAYER_MC_TEMPLATE divider.
        0xCE,
        _TCTR_LO,
        _TCTR_HI_BYTE,  # DEC counter
        0xD0,
        0x08,  # BNE +8 → lean exit (offset 119)
        0xA9,
        REU_PUMP_TICK_DIVIDER,  # LDA #N (offset 112)
        0x8D,
        _TCTR_LO,
        _TCTR_HI_BYTE,  # STA counter
        0x4C,
        0x31,
        0xEA,  # JMP $EA31 (full chain)
        # lean exit (offset 119): ack CIA #1 + JMP to kernal register-restore.
        0xAD,
        0x0D,
        0xDC,  # LDA $DC0D (ack)
        0x4C,
        0x81,
        0xEA,  # JMP $EA81 (RTI)
    ]
)
assert len(REU_IRQ_HANDLER_TRACKED) == 125, (
    "REU_IRQ_HANDLER_TRACKED length changed — BCC offset (currently +10), "
    "chunk-size patch offsets (2, 7, 51, 59, 76, 84), and divider patch "
    "offset (112) must be recomputed."
)
# Six chunk operands here, not two: the length reload plus the src and dst
# advances the tracked variant does by hand (see the layout comment above).
REU_IRQ_HANDLER_TRACKED_CHUNK_OFFSETS = (2, 7, 51, 59, 76, 84)
_assert_chunk_offsets(
    REU_IRQ_HANDLER_TRACKED,
    REU_IRQ_HANDLER_TRACKED_CHUNK_OFFSETS,
    "REU_IRQ_HANDLER_TRACKED",
)


# Same REC pump work as the TRACKED handler but exposed as an RTS-ending
# subroutine. Called from the chunked mhires bank-swap dispatcher between
# every per-frame REC chunk so CIA #1 IRQ events that would otherwise
# collapse against the I-flag (we're already in the raster IRQ handler
# for the full ~18 ms bank-swap) get serviced inline. Without this, the
# pump under-produces by ~43 % and the audio ring drains in ~2.4 sec
# (empirically measured 2026-05-27 via $C200 src-tracker probe).
#
# Construction: bytes 1..104 of REU_IRQ_HANDLER_TRACKED (everything
# between the leading PHA and the PLA at offset 105) plus a trailing
# RTS. The leading PHA is dropped because the caller (chunked bank-swap)
# does not need A preserved across the JSR. The BCC inside the handler
# (originally at offset 93 → target offset 105 / PLA) shifts uniformly
# by −1 to BCC at offset 92 → target offset 104 / RTS. Displacement
# byte (+10) is unchanged because the shift is uniform.
#
# Lives at $C180, uploaded alongside the $C100 handler in
# AudioStreamer.start_for_reu_staged / ._start_mic_for_reu_pump whether or not
# the chunked dispatcher is active.
REU_PUMP_BODY_SUBROUTINE_ADDR = 0xC180
REU_PUMP_BODY_SUBROUTINE = (
    REU_IRQ_HANDLER_TRACKED[1:105] + bytes([0x60])  # RTS
)
assert len(REU_PUMP_BODY_SUBROUTINE) == 105, (
    "REU_PUMP_BODY_SUBROUTINE length changed — the chunked bank-swap "
    "dispatcher in modes_irq.py JSRs to a fixed address ($C180) and the "
    "subroutine must end with RTS at offset 104 so the BCC at offset "
    "92 (displacement +10) lands on it correctly."
)
# The trailing byte must be RTS so the BCC's "no-wrap" early-exit
# returns to the caller correctly.
assert REU_PUMP_BODY_SUBROUTINE[-1] == 0x60, "subroutine must end with RTS"


# Same architecture as the video REU pump above, but the REU source
# side is also a ring (the mic produces samples in real time, so we can't
# preload). Host's sounddevice callback REUWRITEs each encoded chunk into
# the REU mic ring at `AudioStreamer._mic_reu_write_pos`, wrapping at REU_MIC_SIZE. The
# C64-side IRQ handler reads from the same ring at the matched pump rate
# (CIA #1 latch derived from the live NMI latch by
# AudioStreamer._program_reu_pump_rate, same as video), wrapping its
# REU source pointer at REU_MIC_END_HI.
#
# Bootstrap: the entire REU ring is pre-filled with NEUTRAL_SAMPLE so the
# pump's first ~200 ms read silence (not garbage SRAM) while the mic
# warms up; `_mic_reu_write_pos` starts at REU_MIC_BOOTSTRAP_BYTES so the
# first burst of real mic data lands ahead of the pump's read position
# (= steady-state latency of REU_MIC_BOOTSTRAP_BYTES / sample_rate).
#
# 64 KB. The host produces at the exact mic rate, the pump consumes at the
# NMI-matched rate (~0.16% slower on NTSC, faster on PAL) — ~16 B/sec of drift,
# which this size absorbs for hours before the host laps the pump and samples
# start dropping.
# 1 MB into REU. A scene runs at most one pump, so this never coexists with
# the staged-audio upload at REU_AUDIO_BASE — REU_AUDIO_MAX_BYTES is bounded
# by the video staging region instead, which does coexist with it.
REU_MIC_BASE = 0x100000
REU_MIC_SIZE = 0x10000  # 64 KB (several seconds of headroom)
REU_MIC_END = REU_MIC_BASE + REU_MIC_SIZE
REU_MIC_BASE_HI = (REU_MIC_BASE >> 16) & 0xFF
REU_MIC_END_HI = (REU_MIC_END >> 16) & 0xFF
REU_MIC_BOOTSTRAP_BYTES = 1600  # ~133 ms @ 12 kHz; tunes steady-state latency

# Main-RAM REU-source tracker for the mic pump. Three bytes (LO/MI/HI) the
# handler loads into $DF04/$DF05/$DF06 before each trigger, then increments
# by REU_PUMP_CHUNK_SIZE after. Wraps at REU_MIC_END_HI back to REU_MIC_BASE.
#
# Why not just read $DF06 like the dst-wrap path reads $DF03? The U64's REU
# emulation returns GARBAGE in the upper bits of $DF06 read-back ($F8 instead
# of the $00/$10 the LO/HI page actually contains). The dst-side $DF03 reads
# correctly, but the src-side $DF06 doesn't. If the handler trusts that read,
# CMP #reu_end_hi sees $F8 every time, BCC src_done never branches, and the
# wrap-reset block fires on EVERY IRQ — meaning the pump always reads from
# the start of the REU ring (the bootstrap NEUTRAL prefill) and never sees
# the real mic data the host wrote further in. Audio output stays silent.
# Tracking in main RAM bypasses the unreliable register read entirely.
#
# Lives in the $C200 slot just past the 102-byte handler at $C100 (handler
# ends at $C166; slot is in the free $C167-$C1FF region of the audio module's
# $C000-$C2FF allocation). The tracker is REU_AUDIO_SRC_TRACKER_ADDR
# (defined up by REU_IRQ_HANDLER_TRACKED) — both pumps share the same RAM
# slot since a single scene only runs one.

# 6502 IRQ handler at $C100 for the mic pump.
#
# Per-trigger:
#   1. Re-set length register (auto-decremented during the previous transfer)
#   2. LOAD src registers from main-RAM tracker (works around $DF06 garbage)
#   3. Trigger DMA (~128 cyc CPU halt while REU→main runs)
#   4. ADVANCE main-RAM tracker by chunk_size
#   5. SRC WRAP: if tracker HI ≥ REU_MIC_END_HI, reset tracker to REU_MIC_BASE
#   6. DST WRAP: if $DF03 ≥ RING_BUFFER_END_HI, reset dst to RING_BUFFER_ADDR
#      (this side reads $DF03 directly — that register IS reliable)
#   7. Chain to kernal IRQ
#
# Byte layout (offsets relative to $C100):
#   0    PHA
#   1    LDA #<chunk_size / STA $DF07              ┐ re-set length
#   6    LDA #>chunk_size / STA $DF08              ┘
#  11    LDA tracker_lo / STA $DF04                ┐ load src from main RAM
#  17    LDA tracker_mi / STA $DF05                │
#  23    LDA tracker_hi / STA $DF06                ┘
#  29    LDA #$91 / STA $DF01                      ; trigger DMA
#  34    CLC
#  35    LDA tracker_lo / ADC #<chunk_size / STA tracker_lo  ┐ advance tracker
#  43    LDA tracker_mi / ADC #>chunk_size / STA tracker_mi  │
#  51    LDA tracker_hi / ADC #$00 / STA tracker_hi          ┘
#  59    LDA tracker_hi / CMP #reu_end_hi          ; src wrap check
#  64    BCC +15 → offset 81 (dst wrap block)
#  66    LDA #reu_start_hi / STA tracker_hi        ┐ reset tracker to base
#  71    LDA #$00 / STA tracker_mi                 │
#  76    LDA #$00 / STA tracker_lo                 ┘
#  81    LDA $DF03 / CMP #ring_end_hi              ; dst wrap check ($DF03 IS reliable)
#  86    BCC +10 → offset 98 (PLA)
#  88    LDA #ring_start_hi / STA $DF03            ┐ reset dst to RING_BUFFER_ADDR
#  93    LDA #$00 / STA $DF02                      ┘
#  98    PLA / JMP $EA31                           ; chain to kernal IRQ
REU_MIC_IRQ_HANDLER = bytes(
    [
        0x48,  # PHA
        # re-set length (auto-decrements during DMA, must reload):
        0xA9,
        REU_PUMP_CHUNK_SIZE & 0xFF,  # LDA #<chunk_size
        0x8D,
        0x07,
        0xDF,  # STA $DF07
        0xA9,
        (REU_PUMP_CHUNK_SIZE >> 8) & 0xFF,  # LDA #>chunk_size
        0x8D,
        0x08,
        0xDF,  # STA $DF08
        # load src from main-RAM tracker:
        0xAD,
        _TRK_LO,
        _TRK_HI_BYTE,  # LDA tracker_lo
        0x8D,
        0x04,
        0xDF,  # STA $DF04
        0xAD,
        (_TRK_LO + 1) & 0xFF,
        _TRK_HI_BYTE,  # LDA tracker_mi
        0x8D,
        0x05,
        0xDF,  # STA $DF05
        0xAD,
        (_TRK_LO + 2) & 0xFF,
        _TRK_HI_BYTE,  # LDA tracker_hi
        0x8D,
        0x06,
        0xDF,  # STA $DF06
        # trigger DMA:
        0xA9,
        REU_CMD_FETCH_EXEC,  # LDA #$91
        0x8D,
        0x01,
        0xDF,  # STA $DF01
        # advance tracker by chunk_size (16-bit add-with-carry across 3 bytes):
        0x18,  # CLC
        0xAD,
        _TRK_LO,
        _TRK_HI_BYTE,  # LDA tracker_lo
        0x69,
        REU_PUMP_CHUNK_SIZE & 0xFF,  # ADC #<chunk_size
        0x8D,
        _TRK_LO,
        _TRK_HI_BYTE,  # STA tracker_lo
        0xAD,
        (_TRK_LO + 1) & 0xFF,
        _TRK_HI_BYTE,  # LDA tracker_mi
        0x69,
        (REU_PUMP_CHUNK_SIZE >> 8) & 0xFF,  # ADC #>chunk_size
        0x8D,
        (_TRK_LO + 1) & 0xFF,
        _TRK_HI_BYTE,  # STA tracker_mi
        0xAD,
        (_TRK_LO + 2) & 0xFF,
        _TRK_HI_BYTE,  # LDA tracker_hi
        0x69,
        0x00,  # ADC #$00 (carry only)
        0x8D,
        (_TRK_LO + 2) & 0xFF,
        _TRK_HI_BYTE,  # STA tracker_hi
        # src wrap check on tracker_hi:
        0xAD,
        (_TRK_LO + 2) & 0xFF,
        _TRK_HI_BYTE,  # LDA tracker_hi
        0xC9,
        REU_MIC_END_HI,  # CMP #reu_end_hi
        0x90,
        0x0F,  # BCC +15 → offset 81 (dst wrap)
        0xA9,
        REU_MIC_BASE_HI,  # LDA #reu_start_hi
        0x8D,
        (_TRK_LO + 2) & 0xFF,
        _TRK_HI_BYTE,  # STA tracker_hi
        0xA9,
        0x00,  # LDA #$00
        0x8D,
        (_TRK_LO + 1) & 0xFF,
        _TRK_HI_BYTE,  # STA tracker_mi
        0xA9,
        0x00,  # LDA #$00
        0x8D,
        _TRK_LO,
        _TRK_HI_BYTE,  # STA tracker_lo
        # dst wrap check on $DF03 (reliable, same as video handler):
        0xAD,
        0x03,
        0xDF,  # LDA $DF03
        0xC9,
        RING_BUFFER_END_HI,  # CMP #ring_end_hi
        0x90,
        0x0A,  # BCC +10 → offset 98 (PLA)
        0xA9,
        RING_BUFFER_HI,  # LDA #ring_start_hi
        0x8D,
        0x03,
        0xDF,  # STA $DF03
        0xA9,
        0x00,  # LDA #$00
        0x8D,
        0x02,
        0xDF,  # STA $DF02
        # end:
        0x68,  # PLA
        0x4C,
        0x31,
        0xEA,  # JMP $EA31
    ]
)
assert len(REU_MIC_IRQ_HANDLER) == 102, (
    "REU_MIC_IRQ_HANDLER length changed — BCC offsets (currently +15 src, +10 dst) "
    "must be recomputed to land on the dst-wrap LDA $DF03 and trailing PLA."
)


# Each voice: gate (bit 0) + pulse waveform (bit 6) + TEST bit locked (bit 3).
# With test bit held, the oscillator is frozen at zero and the pulse output
# is a steady DC level — this gives the master volume DAC a constant bias to
# scale. Sustain=$F keeps the ADSR envelope D/A fully open so the DC is at
# maximum amplitude.
SID_DIGIBOOST_CONTROL = 0x49  # gate + pulse + test
SID_DIGIBOOST_SR = 0xF0  # sustain=$F, release=0
SID_GATE_OFF = 0x40  # pulse waveform, gate=0 → envelope release

# Park all 3 voices as steady DC sources (pulse + TEST + GATE, ADSR held) with
# voices 1+2 routed through the analog filter. With this env, the FULL $D418
# byte written per NMI sample — volume nibble (0-3) + filter HP/BP/LP mode bits
# (4-6) + "voice-3 OFF" (7) — additively/subtractively re-routes the parked DC
# voices to ~256 distinct output levels (~6-7 effective bits) instead of the 16
# the volume nibble gives alone. Written ONCE; the per-sample NMI handler is
# unchanged. Mutually exclusive with digi-boost (both park the voices, for
# different DAC schemes). See dac_curves.py for the amplitude→$D418 tables.
SID_MAHONEY_CONTROL = 0x49  # pulse + TEST (osc frozen at DC) + GATE
SID_MAHONEY_AD = 0x0F  # attack=0, decay=15
SID_MAHONEY_SR = 0xFF  # sustain=15, release=15
SID_MAHONEY_RES_FILT = 0x03  # route voices 1+2 through filter, resonance=0


def servo_period(
    gap: int,
    integ: float,
    *,
    chunk_period: float,
    target_gap: int = HOST_DMA_SERVO_TARGET_GAP,
    kp: float = HOST_DMA_SERVO_KP,
    ki: float = HOST_DMA_SERVO_KI,
) -> tuple[float, float]:
    """PI controller on the host-DMA worker's per-chunk pace period.

    ``gap`` = (write_addr - R) % RING_BUFFER_SIZE — how far the write head W
    leads the NMI read pointer R. ``integ`` is the integrator state carried
    across chunks. Error ``e = gap - target_gap``; positive e means W is too far
    ahead, so we *lengthen* the period to slow W back toward target_gap. Returns
    ``(period_eff, new_integ)``.

    Proportional control alone turns the unbounded open-loop drift into a bounded
    constant phase offset (no lap); the integral term drives that residual offset
    to zero so the gap parks at target_gap. Pure (no I/O, no clock) so the
    control math is unit-testable without hardware.
    """
    e = gap - target_gap
    integ += e
    # Anti-windup: bound the integral's *contribution* to ±INTEG_CLAMP·period.
    if ki > 0:
        integ_limit = HOST_DMA_SERVO_INTEG_CLAMP * chunk_period / ki
        integ = max(-integ_limit, min(integ_limit, integ))
    period = chunk_period + kp * e + ki * integ
    period = max(
        HOST_DMA_SERVO_PERIOD_MIN_FRAC * chunk_period,
        min(HOST_DMA_SERVO_PERIOD_MAX_FRAC * chunk_period, period),
    )
    return period, integ


def nmi_rate_step(
    r_rate_ema: float,
    latch: int,
    *,
    nominal_latch: int,
    ceiling_latch: int,
    target_rate: float,
    deadband_frac: float = NMI_RATE_LOOP_DEADBAND_FRAC,
    coarse_zone_frac: float = NMI_RATE_LOOP_COARSE_ZONE_FRAC,
    max_coarse_step: int = NMI_RATE_LOOP_MAX_COARSE_STEP,
) -> int:
    """One decision of the adaptive NMI-rate loop: nudge ``latch`` so the measured
    consumer rate ``r_rate_ema`` moves toward ``target_rate`` (= sample_rate).

    Rate and latch are INVERSE (NMI period = latch+1 cycles), so R too slow
    (positive error) ⇒ DECREASE latch (faster NMI). The clamp range is
    ``[ceiling_latch, nominal_latch]``: ``nominal_latch`` is the rate floor (the
    latch for sample_rate, reached when there are no bus halts) and
    ``ceiling_latch`` is the fastest safe latch (handler cycle budget). The loop
    can therefore only SPEED UP from nominal toward the ceiling to overcome
    halt-induced tick loss; it can never push past the overrun guard.

    Deadband (≥ one latch quantum) parks the integer latch instead of
    limit-cycling. Outside ``coarse_zone_frac`` a proportional step (capped)
    acquires fast; inside it moves ±1 so steady-state pitch steps are inaudible.
    Pure (no I/O) for unit testing — mirrors ``servo_period``."""
    if r_rate_ema <= 0 or target_rate <= 0:
        return latch
    err_frac = (target_rate - r_rate_ema) / target_rate
    if abs(err_frac) <= deadband_frac:
        return latch
    if abs(err_frac) > coarse_zone_frac:
        step = min(max_coarse_step, max(1, round(abs(err_frac) * (latch + 1))))
    else:
        step = 1
    # positive err (consumer too slow) → smaller latch (faster NMI)
    new_latch = latch - step if err_frac > 0 else latch + step
    return max(ceiling_latch, min(nominal_latch, new_latch))
