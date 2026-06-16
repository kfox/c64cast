"""NMI-driven 4-bit SID DAC audio via the master volume register ($D418).

A small 6502 routine at $C020 pulls one sample per NMI from an 8 KB ring
buffer at $4000-$5FFF, writing the low nibble to $D418. Python feeds the
ring buffer via Socket DMA. CIA #2 Timer A fires NMIs at the configured
sample rate (default 8 kHz).

The ring lives at $4000 (not $8000) so it sits outside VIC banks 0 and 2
— the two banks with kernal char-ROM mapped at $1000/$9000, which is what
PETSCII char modes need. With the audio ring out of those banks, video
double-buffering can swap $DD00 between bank 0 and bank 2 without VIC
trying to render audio samples as garbage screen data. The 6510 sees
$4000-$5FFF as normal main RAM regardless of VIC bank selection. The
relocation cost is one address change here + matched edits in the NMI
handler (read addr, end-of-ring compare, wrap-reset literal) and REU IRQ
handler (which already uses the RING_BUFFER_* constants, so it just
follows). Bitmap modes that want VIC bank 1 ($4000-$7FFF) need a future
relocation; PETSCII never selects bank 1 (no char-ROM mapping there).

Why not PWM via $D402?
  Hardware testing on a real 6581 confirmed two fatal problems with NMI-based
  pulse-width modulation on an active display:
  1. At 8 kHz NMI rate, the PWM carrier sits at 8 kHz — fully within human
     hearing. Spectral capture showed the carrier 9 dB louder than the audio.
  2. At 16 kHz NMI rate, VIC-II badlines (40 stolen cycles in a 63-cycle NMI
     period) cause the NMI handler to overrun and queue. Queued NMIs then fire
     back-to-back at the handler's completion speed (53 cycles), stretching
     audio samples and lowering the perceived pitch by ~4.5%. Captured 440 Hz
     tones appeared at 421 Hz.
  $D418 4-bit avoids both problems: no carrier frequency, and timing jitter
  from badlines only shifts the voltage step by a fraction of a sample period
  without distorting pitch.

SID digi-boost (optional): lock all 3 voices into a steady DC pulse so the
ADSR envelope D/As feed a constant bias into the master mixer. The $D418 DAC
trick scales this bias; without it (or the 6581's natural ADSR DC offset),
writes barely move the speaker on 8580s / emulated SIDs.

The worker uses strict absolute pacing: chunk writes land exactly chunk_period
after the previous one, never snapping forward on overrun. The 8 KB ring
(~1 s @ 8 kHz) absorbs DMA stalls and GC pauses.

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
muffled with a strong tremolo-buzz on every consonant.
"""

from __future__ import annotations

import dataclasses
import logging
import queue
import threading
import time
from typing import Any

import numpy as np

from .backend import C64Backend
from .c64 import CIA1, CIA2, CLOCK_NTSC, CLOCK_PAL, KERNAL, REU, SID, VECTORS
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

# $D418 DAC NMI routine assembled at $C020 (32 bytes).
# Saves/restores only A (X and Y are not touched), saving 8 cycles vs the
# original version that preserved all three registers.
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
# Where the NMI routine lives in C64 RAM (audio.py "owns" $C000-$C04F).
NMI_ROUTINE_ADDR = 0xC020

# Audio ring buffer: 8 KB at $4000-$5FFF. The NMI routine reads one sample
# per fire and the Python worker refills the buffer in chunk_size pieces,
# wrapping at the end. 8 KB = ~1 s @ 8 kHz gives the paced worker enough
# slack that occasional latency spikes (DMA stalls, GC pauses) don't let
# NMI's read pointer catch up to the worker's write pointer and start
# replaying stale audio (audible as a brief echo).
#
# $4000 (not $8000) so the ring sits in VIC bank 1, which c64cast never
# selects — banks 0 ($0000-$3FFF) and 2 ($8000-$BFFF) are the only banks
# with kernal char-ROM mapped (at $1000 / $9000 respectively), and PETSCII
# char modes need that mapping. Keeping audio out of bank 2 unblocks the
# bank-0↔bank-2 double-buffer swap used by the REU-staged display modes.
RING_BUFFER_ADDR = 0x4000
RING_BUFFER_SIZE = 0x2000
RING_BUFFER_END = RING_BUFFER_ADDR + RING_BUFFER_SIZE
RING_BUFFER_HI = RING_BUFFER_ADDR >> 8
RING_BUFFER_END_HI = RING_BUFFER_END >> 8

NEUTRAL_SAMPLE = 7  # mid-scale 4-bit value; keeps the speaker cone centered

# CIA #2 control words for NMI bring-up / teardown.
#  - DISABLE: clear all five IRQ-source bits in ICR (high bit = 0 → clear).
#  - ICR_CLEAR: companion write to acknowledge any pending NMI.
#  - ENABLE_TIMER_A_NMI: set bit 7 + bit 0 (enable timer-A IRQ source).
#  - TIMER_A_CONTINUOUS: continuous mode, start (CRA bits 0+4).
CIA2_ICR_DISABLE_ALL = 0x7F
CIA2_ICR_CLEAR = 0x00
CIA2_ICR_ENABLE_TIMER_A_NMI = 0x81
CIA2_TIMER_A_CONTINUOUS = 0x11

# Float-sample → 4-bit volume code: (x + 1) * VOLUME_SCALE, clipped to
# [0, MAX_VOLUME]. Centers a [-1, 1] input on 7.5 → DAC ~half-scale.
DAC_VOLUME_SCALE = 7.5
DAC_MAX_VOLUME = 15
INT16_FULL_SCALE = 32768.0  # divisor to map int16 → float [-1, 1]
INT16_MAX = 32767  # int16 saturation bounds (np.iinfo(np.int16))
INT16_MIN = -32768


def encode_floats_to_dac(
    floats: np.ndarray,
    *,
    dither: bool,
    rng: np.random.Generator | None = None,
) -> np.ndarray:
    """Quantize float audio samples in [-1, 1] to 4-bit SID DAC codes (the
    0..15 volume nibble), as a uint8 array. Single source of truth for the
    DAC encoding shared by every input path (host-DMA mic, REU mic, offline
    video pre-encode) — the quantization math must stay identical across
    them or REU-mode and host-mode levels would silently diverge.

    TPDF dither (``dither=True``): a triangular ±1 DAC-LSB random offset added
    pre-quantization, decorrelating the rounding error from the signal. At
    4 bits the coarse rounding otherwise produces signal-correlated harmonic
    distortion (buzz/chop on speech); dither turns the same total error into
    smooth white-noise hiss, perceptually less intrusive. The triangular shape
    comes from subtracting two independent uniform [0, 1) draws (Wannamaker
    1992). Exact-zero input samples skip dither so gated silence stays silent
    (the mic + AVFileSource noise gates zero the noise floor — adding hiss
    there would repaint what they cleared).

    rng: when None (default) uses numpy's legacy global RNG, matching the
    realtime callback paths; pass a Generator for thread-local / reproducible
    dither (the offline pre-encode path does)."""
    vol_float = (floats + 1.0) * DAC_VOLUME_SCALE
    if dither:
        if rng is None:
            d = np.random.random_sample(floats.shape).astype(np.float32) - np.random.random_sample(
                floats.shape
            ).astype(np.float32)
        else:
            d = rng.random(floats.shape, dtype=np.float32) - rng.random(
                floats.shape, dtype=np.float32
            )
        d[floats == 0] = 0.0
        vol_float = vol_float + d
    return np.clip(vol_float, 0, DAC_MAX_VOLUME).astype(np.uint8)


# Queue + backpressure sizing.
AUDIO_QUEUE_MAX_BLOBS = 256  # outer cap (per-blob, not per-sample)
MAX_QUEUED_SAMPLES = 16384  # soft cap (~2 s @ 8 kHz)
PREBUFFER_CHUNKS = 6  # chunks to buffer before starting NMI
QUEUE_PUT_TIMEOUT_S = 0.2
BACKPRESSURE_SPIN_S = 0.005  # sleep between full-queue retries

# Fixed-ratio resampler (host-DMA path) — the pitch-compensation mechanism.
# The host-DMA servo locks playback to the bus-halt-throttled NMI consumer, so a
# heavy display mode plays a touch slow (the residual the per-mode pitch_mult was
# meant to cancel). Instead of speeding the NMI up (a faster CIA #2 latch — extra
# C64 stress, and it chases a read-pointer rate that is biased low under bus load),
# we decimate the source float stream by a FIXED per-mode ratio = 1 / pitch_mult
# before the 4-bit encode: dropping that fraction of samples makes the same NMI
# consumption rate cover more source seconds, i.e. plays `pitch_mult`× faster.
# The ratio is a fixed scalar set once per scene (NOT measured from R), so it can't
# over-correct the way the R-driven loop did (capture-verified +10.8% fast). numpy
# linear interpolation — at 4-bit output (~24 dB) linear's alias/droop floor sits
# far below the quantization floor and the ratios are mild, so a polyphase kernel
# buys nothing audible.
RESAMPLE_RATIO_MIN = 0.85  # never decimate harder than ~15% (sanity floor)
RESAMPLE_DEADBAND = 0.002  # within 0.2% of 1.0 → bit-exact passthrough

# Pre-quantization sample tap. Holds the most recent SAMPLE_TAP_SIZE float
# samples in [-1, 1] for FFT-based overlays (spectrum analyzers). Sized to
# cover ~256 ms at 8 kHz, giving a usable FFT down to ~30 Hz.
SAMPLE_TAP_SIZE = 2048


# --- REU-staged audio pump -----------------------------------------------
# Architecture: the entire pre-recorded audio track is preloaded into the U64's
# REU (RAM Expansion Unit) FPGA SRAM via socket DMA opcode 0xFF07 REUWRITE.
# Once loaded, a small 6502 IRQ handler at $C100 triggers REU→ring DMAs at
# the kernal IRQ rate (~62 Hz after CIA #1 reprogramming) to refill the audio
# ring buffer at $4000. NMI continues to consume the ring at 8 kHz exactly as
# in the existing host-DMA path. The key win: host-side DMAWRITEs to the ring
# (which audibly perturb SID output on real hardware — the "gurgling" artifact)
# are replaced entirely by C64-side REU DMAs whose deterministic CIA timing
# produces perceptually cleaner audio.
#
# REU mode is opt-in via [audio].use_reu_pump in TOML, and only the
# VideoScene branch uses it today (whole track known upfront).

REU_PUMP_HANDLER_ADDR = 0xC100  # IRQ handler lives here; $C020 NMI handler stays
REU_AUDIO_BASE = 0x000000  # REU offset where preloaded audio starts
REU_PUMP_CHUNK_SIZE = 128  # bytes per IRQ-triggered REU DMA (default)
REU_UPLOAD_SLICE = 32 * 1024  # bytes per socket REUWRITE (one per slice)

# Write-behind-read margin for the pump's initial pointer placement.
#
# The pump (write pointer W) and the NMI DAC reader (read pointer R) both
# walk the 8 KB ring at the same average rate, so the mapping is constant:
# REU sample N always lands at ring position (N mod RING_BUFFER_SIZE). What
# matters for correctness is the *pointer gap* between W and R — the safety
# slack before timing jitter lets one cross the other:
#
#   * R catches W (R laps the write pointer): NMI reads positions the pump
#     hasn't refreshed yet → stale data from the previous ring lap →
#     audible "echo"/overlap (the user's chief audio complaint).
#   * W catches R (pump overwrites just ahead of the reader): NMI reads
#     next-lap (future) samples mixed with current-lap → the same overlap.
#
# Both failure modes happen on this hardware: bus halts (mhires bank-swap
# REC DMA, Phase 9) starve EITHER NMI ticks (R slows → W catches R) OR the
# CIA #1 pump IRQ (W slows → R laps W), depending on which IRQ source the
# halt window collapses. The original bring-up seeded W and R at the SAME
# position (dst = ring start, src = RING_BUFFER_SIZE), leaving only the
# ~50 ms NMI head-start (~400 bytes) of slack — any jitter spike past that
# crossed the pointers and produced the echo.
#
# Seeding W exactly half a ring behind R (dst = src = RING_BUFFER_SIZE/2)
# is the symmetric optimum: ~0.5 s of jitter headroom in BOTH directions
# before a crossing. Data continuity is unchanged because src offset ≡ dst
# position (mod ring) — the pump just redundantly re-writes the upper half
# of the prefill with identical bytes once at startup, then runs steadily
# half a ring behind the reader. See start_for_reu_staged step 3.
REU_PUMP_INITIAL_MARGIN = RING_BUFFER_SIZE // 2  # 4096 B ≈ 0.5 s @ 8 kHz

# When the active display mode halts the C64 bus heavily (mhires DMAWRITE
# is ~300 KB/sec which makes NMI lose ~30% of its ticks — measured at
# 4020 Hz effective on real U64 hardware, 2026-05-26), the default
# chunk_size of 128 over-produces 2x (8 KB/sec pump vs ~4 KB/sec NMI
# consumption) and overflows the ring buffer in ~2 sec. The actual rate
# also varies with what video is doing. 80 is a compromise: slight
# under-production for the worst-case (all-frame full bitmap) means NMI
# pads NEUTRAL on a few percent of samples (mild background hiss) but the
# ring never overflows. For PETSCII / Blank scenes (no bitmap DMA),
# the default 128 = perfect 8 kHz match.
REU_PUMP_CHUNK_SIZE_HEAVY_BUS = 80

# CIA #1 Timer A latch for matched pump rate. Pump period = chunk × NMI
# period. With chunk = 128 and NMI Timer A latch = 127 (period = 128 cyc),
# pump period = 128 × 128 = 16384 cyc. latch = 16383 = $3FFF. This holds for
# both NTSC and PAL because it's a ratio of periods, not an absolute time.
REU_PUMP_CIA1_LATCH = 0x3FFF

# NTSC kernal default CIA #1 Timer A latch ($4025 = 16421 → ~60.0 Hz),
# restored when the REU pump disarms so the next kernal IRQ runs at the
# stock jiffy rate. PAL's default differs slightly, but the timer keeps
# running either way and the next reset clears it, so the NTSC value is fine.
CIA1_TIMER_A_LATCH_KERNAL_NTSC = 0x4025

# --- C64-side REU-pump rate governor -------------------------------------
# The pump (CIA #1 rate) produces at the fixed nominal rate; video DMA
# bus-halts throttle the NMI *reader* below nominal, so the pump out-produces
# it and the write head laps the reader every ~15-23s = echo (see the
# reu_pump_ring_drift memory + reu_margin_probe.py). An earlier HOST-side
# servo trimmed the CIA #1 latch over REST to match rates — it locked the
# phase, but each CIA-latch reprogram is a bus write that audibly glitches the
# pump cadence (the user heard "regular choppiness"). The fix is to regulate
# ON the C64 with ZERO host bus writes during playback: the pump's own IRQ
# reads the NMI read pointer R and *skips its chunk* whenever the write head
# has gotten too far ahead. The nominal pump rate is always >= the (only ever
# throttled) consumer, so skip-when-ahead is sufficient — it caps the gap near
# half a ring and never underruns.
#
# Gap is measured in 256-byte (HI-byte) units. The ring spans 32 HI values
# ($40-$5F), so gap_hi = (dst_hi - R_hi) & $1F (0-31). Masking to 5 bits also
# discards any garbage the U64 REU returns in the upper bits of the dst HI
# register read-back. The skip threshold is half a ring (REU_PUMP_INITIAL_MARGIN
# >> 8 = 16), matching the bring-up seed, so the gap parks symmetrically with
# ~4 KB of headroom before either a lap (W catches R) or an underrun (R catches
# W). Bang-bang control parks the gap just under the threshold.
REU_GOVERNOR_GAP_THRESHOLD_HI = REU_PUMP_INITIAL_MARGIN >> 8  # 16 (= half ring)
# NMI read pointer HI byte (R_hi): NMI_ROUTINE self-modifying operand at
# $C026. The plain governor reads this directly on-chip; the host never writes.
READ_PTR_HI_ADDR = NMI_ROUTINE_ADDR + 6  # $C026

# --- Host-DMA pacing servo (closed-loop W->R rate match) -----------------
# The host-DMA worker (_worker) paces ring writes strictly to wall-clock, so the
# write head W advances at exactly sample_rate B/s. The NMI reader R, however,
# loses ~4% of its ticks to video DMA bus-halts (measured ~7690 B/s vs the
# 8000 B/s producer), so W out-produces R by ~310 B/s and laps the 8 KB ring
# every ~26s = audible echo (same mechanism as the REU governor above, but here
# W is software-paced). Because W is paced purely by time.sleep, we can close
# the loop with ZERO C64 writes (unlike the abandoned REU host servo that
# reprogrammed a CIA latch over the bus and glitched audibly): the worker reads
# R once per chunk and runs a PI controller that stretches/shrinks the per-chunk
# pace so the ring gap (W-R) locks near half a ring. See the reu_pump_ring_drift
# memory + scripts/diags/hostdma_drift_probe.py.
READ_PTR_LO_ADDR = NMI_ROUTINE_ADDR + 5  # $C025 (R operand low byte)
HOST_DMA_SERVO_TARGET_GAP = RING_BUFFER_SIZE // 2  # 4096 B (half ring)
# Gains are HW-empirical (TUNABLE). Drift to cancel ~310 B/s => a steady period
# stretch of ~+5 ms/chunk (slows W from 8000 to ~7690 B/s). KP=5e-6 s/byte makes
# a 1000-byte phase error add +5 ms (recovers in ~1-2 s); KI (an order below)
# nulls the residual fixed offset proportional control alone would leave, parking
# the gap at TARGET_GAP rather than at a constant offset.
HOST_DMA_SERVO_KP = 5e-6  # s/byte            (HW-TUNABLE)
HOST_DMA_SERVO_KI = 5e-7  # s/(byte*chunk)    (HW-TUNABLE)
HOST_DMA_SERVO_INTEG_CLAMP = 0.5  # max |ki*integ|, frac of chunk_period
HOST_DMA_SERVO_PERIOD_MIN_FRAC = 0.5
HOST_DMA_SERVO_PERIOD_MAX_FRAC = 1.5

# REC command byte for REU DMA: bit 7 = exec, bit 4 = FF00 disable (execute
# immediately, no $FF00 trigger needed), bits 1:0 = 01 = REU → C64 fetch.
# Autoload bit (5) is OFF so the source address auto-increments across triggers.
# Single source of truth is c64.REU.CMD_FETCH_EXEC — aliased here only so the
# 6502 byte arrays below read with a local name (the value can't drift: it's
# the imported constant, not a re-typed literal).
REU_CMD_FETCH_EXEC = REU.CMD_FETCH_EXEC  # $91

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
# 33; an earlier dev iteration with +8 landed in the middle of STA $DF02 and
# the CPU JAMmed on the `$02` byte (KIL/HLT opcode), silencing all subsequent
# audio. The assertion below catches length mismatches; if you edit the bytes,
# verify the branch targets manually.
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


# --- Plain governor handler (skip-when-ahead, zero host writes) -----------
# REU_IRQ_HANDLER + an 18-byte governor prefix. Before pumping, read the
# write head (dst HI, $DF03) and the NMI read pointer (R HI, $C026), compute
# the ring gap in 256-byte units, and if the write head is already >= half a
# ring ahead, SKIP this chunk (don't trigger, don't advance) so the reader
# catches up. Otherwise fall through to the unmodified pump body. Net effect:
# the gap self-regulates near half a ring with no CIA reprogramming and no
# host bus traffic — eliminating both the echo and the servo's choppiness.
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
assert len(REU_IRQ_HANDLER_GOVERNOR) == 18 + 36, (
    "REU_IRQ_HANDLER_GOVERNOR length changed — the governor prefix is 18 bytes "
    "(BCC +4 over the 4-byte skip block) followed by REU_IRQ_HANDLER[1:]."
)
# Pump body must start exactly at offset 18 (the BCC +4 target).
assert REU_IRQ_HANDLER_GOVERNOR[18] == REU_IRQ_HANDLER[1], (
    "governor pump-body offset drifted from the BCC +4 target (18)"
)


# --- Main-RAM REU source tracker (shared between mic + tracked video) ---
# Lives in the $C200 slot just past the audio handler region ($C100-$C1FF).
# Both the mic pump and the tracked video pump load $DF04/$DF05/$DF06
# from this 3-byte tracker every IRQ. A single scene runs at most one of
# the two pumps, so the shared address is safe.
REU_AUDIO_SRC_TRACKER_ADDR = 0xC200
_TRK_LO = REU_AUDIO_SRC_TRACKER_ADDR & 0xFF
_TRK_HI_BYTE = (REU_AUDIO_SRC_TRACKER_ADDR >> 8) & 0xFF

# Backwards-compatible alias retained because the mic handler block below
# references the original name in its assembled bytes.
REU_MIC_SRC_TRACKER_ADDR = REU_AUDIO_SRC_TRACKER_ADDR


# --- Tick-divider state for tracked REU pump (lean-exit pattern) ---------
# Borrowed from the SID player (api.py SID_PLAYER_MC_TEMPLATE): rather
# than chain to the full kernal IRQ tail ($EA31: SCNKEY + UDTIM + cursor
# blink) on every CIA #1 tick, the handler DECs a counter and only chains
# every Nth tick. The other N-1 ticks take a lean exit (LDA $DC0D / JMP
# $EA81): ack CIA #1, restore registers, RTI. Cuts kernal-tail work by
# (N-1)/N, and — more importantly for mhires — cuts cursor-blink writes
# into the cell at $0400+cursor_pos from ~99 Hz to ~33 Hz. In mhires that
# cell is a *color attribute* (c1/c2 packed nibbles), so each blink flips
# a cell's color; reducing the rate proportionally reduces visible
# flicker. Counter byte lives at $C205 (just past the 5-byte src/dst
# tracker at $C200-$C204).
REU_PUMP_TICK_COUNTER_ADDR = 0xC205
_TCTR_LO = REU_PUMP_TICK_COUNTER_ADDR & 0xFF
_TCTR_HI_BYTE = (REU_PUMP_TICK_COUNTER_ADDR >> 8) & 0xFF
# N=3 → chain every 3rd tick → kernal tail at ~33 Hz with chunk=80
# (CIA #1 @ 100 Hz). Plenty for the 10 Hz keyboard poller and SCNKEY's
# $028D update; well below the 60 Hz the kernal expects but no service
# depends on the exact rate. Capped at 8 in spirit with the SID player —
# higher Ns would mean SCNKEY can't keep up with held keys.
REU_PUMP_TICK_DIVIDER = 3


# --- Tracked video REU pump (coexists with REU bank-swap video) -----
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
# Used INSTEAD OF REU_IRQ_HANDLER when start_for_reu_staged is called with
# skip_irq_vector_hook=True (i.e. when the display mode's merged bank-swap
# dispatcher owns $0314 and the audio handler runs via that dispatcher's
# JMP $C100 fall-through). The plain handler stays in service for the
# solo audio path so we don't risk regression on the proven baseline.
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
        # exit the other N-1. Borrowed from SID player (api.py:089e97a).
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


# --- Pump body subroutine (for chunked bank-swap inline call) -------------
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
# Lives at $C180. Uploaded alongside the $C100 handler in
# start_for_reu_staged / _start_mic_for_reu_pump regardless of whether
# the chunked dispatcher is active — 105 bytes of harmless data in RAM
# if never JSR'd.
REU_PUMP_BODY_SUBROUTINE_ADDR = 0xC180
REU_PUMP_BODY_SUBROUTINE = (
    REU_IRQ_HANDLER_TRACKED[1:105] + bytes([0x60])  # RTS
)
assert len(REU_PUMP_BODY_SUBROUTINE) == 105, (
    "REU_PUMP_BODY_SUBROUTINE length changed — the chunked bank-swap "
    "dispatcher in modes.py JSRs to a fixed address ($C180) and the "
    "subroutine must end with RTS at offset 104 so the BCC at offset "
    "92 (displacement +10) lands on it correctly."
)
# The trailing byte must be RTS so the BCC's "no-wrap" early-exit
# returns to the caller correctly.
assert REU_PUMP_BODY_SUBROUTINE[-1] == 0x60, "subroutine must end with RTS"


# --- REU-staged live-mic pump --------------------------------------------
# Same architecture as the video REU pump above, but the REU source
# side is also a ring (the mic produces samples in real time, so we can't
# preload). Host's sounddevice callback REUWRITEs each encoded chunk into
# the REU mic ring at `_mic_reu_write_pos`, wrapping at REU_MIC_SIZE. The
# C64-side IRQ handler reads from the same ring at the matched pump rate
# (CIA #1 latch = REU_PUMP_CIA1_LATCH, same as video), wrapping its
# REU source pointer at REU_MIC_END_HI.
#
# Bootstrap: the entire REU ring is pre-filled with NEUTRAL_SAMPLE so the
# pump's first ~200 ms read silence (not garbage SRAM) while the mic
# warms up; `_mic_reu_write_pos` starts at REU_MIC_BOOTSTRAP_BYTES so the
# first burst of real mic data lands ahead of the pump's read position
# (= steady-state latency of REU_MIC_BOOTSTRAP_BYTES / sample_rate).
#
# Sized for 64 KB / 8 sec @ 8 kHz — generous burst headroom. The host
# produces at exact mic rate; the pump consumes at NMI-matched rate
# (~0.16% slower than mic on NTSC, faster on PAL). The small mismatch
# eats / produces ~16 B/sec of drift; the ring absorbs hours of mismatch
# before host catches up to pump (then samples drop). For typical short
# sessions this is invisible; for very long sessions a periodic resync
# would be needed (future work).
REU_MIC_BASE = 0x100000  # 1 MB into REU — well above the audio region
REU_MIC_SIZE = 0x10000  # 64 KB = 8 sec @ 8 kHz
REU_MIC_END = REU_MIC_BASE + REU_MIC_SIZE
REU_MIC_BASE_HI = (REU_MIC_BASE >> 16) & 0xFF
REU_MIC_END_HI = (REU_MIC_END >> 16) & 0xFF
REU_MIC_BOOTSTRAP_BYTES = 1600  # 200 ms @ 8 kHz; tunes steady-state latency

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
# $C000-$C2FF allocation). REU_MIC_SRC_TRACKER_ADDR is now an alias for
# REU_AUDIO_SRC_TRACKER_ADDR (defined up by REU_IRQ_HANDLER_TRACKED) —
# both pumps share the same RAM slot since a single scene only runs one.

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


# --- SID digi-boost control bytes ----------------------------------------
# Each voice: gate (bit 0) + pulse waveform (bit 6) + TEST bit locked (bit 3).
# With test bit held, the oscillator is frozen at zero and the pulse output
# is a steady DC level — this gives the master volume DAC a constant bias to
# scale. Sustain=$F keeps the ADSR envelope D/A fully open so the DC is at
# maximum amplitude.
SID_DIGIBOOST_CONTROL = 0x49  # gate + pulse + test
SID_DIGIBOOST_SR = 0xF0  # sustain=$F, release=0
SID_GATE_OFF = 0x40  # pulse waveform, gate=0 → envelope release


def _servo_period(
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
        sid_filter_cutoff: int = 0,
        use_reu_pump: bool = False,
        reu_pump_governor: bool = True,
        host_dma_servo: bool = True,
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
        # PI controller (_servo_period) on its sleep so the ring gap locks near
        # half a ring instead of free-running and lapping (~26s echo). Pure
        # host-side timing — no C64 writes. False = open-loop wall-clock pacing
        # (original drift/echo) for A/B. Does not affect the REU pump path.
        self.host_dma_servo = host_dma_servo
        # PI integrator state for the host-DMA servo (worker-thread-only, so no
        # lock needed). Reset to 0 each time the NMI consumer starts.
        self._servo_integ = 0.0
        # Host-DMA servo gap telemetry (write head's lead over R, in bytes),
        # for non-ears verification via the drift probe / stop() summary. -1 =
        # no servo sample taken yet this session.
        self._servo_gap_min = -1
        self._servo_gap_max = -1
        self._servo_gap_last = -1
        # REU pump state: tracked so stop() can do the right teardown.
        # _reu_pump_armed flips True between arm_reu_pump and disarm_reu_pump.
        # _reu_pump_start_time supports position_seconds() in REU mode where
        # the host-side queue counter doesn't apply (NMI consumes from C64
        # ring, host never sees the samples).
        self._reu_pump_armed = False
        self._reu_pump_start_time = 0.0
        self._reu_pump_total_samples = 0
        # _nmi_latch is the CIA #2 Timer A latch the NMI consumer runs at (set
        # by _start_nmi_timer); the pump's nominal CIA #1 latch derives from it
        # so the producer/consumer period ratio stays exact.
        self._nmi_latch = 0
        # Host-DMA-servo pitch compensation, fixed-ratio resampler edition.
        # _resample_ratio is the decimation ratio in (0, 1]: 1.0 = passthrough,
        # <1.0 drops that fraction of source samples so playback runs 1/ratio×
        # faster (cancels the bus-halt slowdown). set_pitch_compensation_for_mode
        # sets it from the per-mode pitch_mult (ratio = 1 / mult) at scene setup;
        # it is a plain float read by the producer thread (atomic under the GIL).
        # _resample_phase + _resample_prev_tail are the producer-owned seam state
        # for the cross-chunk linear interpolation (reset at session start +
        # stop()); never touched by the worker, so there is no cross-thread race.
        self._resample_ratio = 1.0
        self._resample_phase = 0.0
        self._resample_prev_tail = np.zeros(0, dtype=np.float32)
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
        # Per-item queue: each item is a (payload, src_weight) tuple — a
        # pre-encoded bytes blob of 4-bit volume codes plus the number of SOURCE
        # samples it represents. src_weight == len(payload) when the resampler is
        # a no-op; under decimation the payload is shorter than src_weight. The
        # worker credits _queued_samples (a SOURCE-sample tally for
        # position_seconds + backpressure) by src_weight, not bytes, so the A/V
        # master clock stays on the source timeline regardless of decimation.
        # This collapses the old per-sample put/get (which hit ~88K lock
        # acquisitions/sec on a 44.1 kHz PyAV demux) to one lock per audio chunk.
        self.q: queue.Queue[tuple[bytes, int]] = queue.Queue(maxsize=AUDIO_QUEUE_MAX_BLOBS)
        self._queued_samples = 0
        # Cap the buffered audio at ~2 s @ 8 kHz so a stalled consumer
        # doesn't accumulate a wall of stale audio.
        self._max_queued_samples = MAX_QUEUED_SAMPLES
        self.running = False
        self.chunk_size = 1024
        self.sensitivity = 1.0
        self.noise_gate = 0.05
        self.mic_stream: Any = None
        self._worker_thread: threading.Thread | None = None

        # Audio-master clock bookkeeping (used by PyAV-driven scenes).
        self._pushed_count = 0

        # Sample tap for FFT overlays. Lockless write from input threads,
        # locked read from the render thread — readers tolerate a torn frame
        # because the next FFT is ~16 ms away.
        self._tap_buf = np.zeros(SAMPLE_TAP_SIZE, dtype=np.float32)
        self._tap_write = 0
        self._tap_lock = threading.Lock()

    # ---- 6502 bring-up -------------------------------------------------------
    def _upload_nmi_and_buffers(self) -> None:
        nmi = bytearray(NMI_ROUTINE)
        nmi[NMI_ROUTINE_PATCH_OFFSET_READ_HI] = RING_BUFFER_HI
        nmi[NMI_ROUTINE_PATCH_OFFSET_WRAP_HI] = RING_BUFFER_END_HI
        nmi[NMI_ROUTINE_PATCH_OFFSET_RESET_HI] = RING_BUFFER_HI
        self.api.write_memory_file(f"{NMI_ROUTINE_ADDR:04X}", bytes(nmi))
        self.api.write_memory_file(
            f"{RING_BUFFER_ADDR:04X}", bytes([NEUTRAL_SAMPLE] * RING_BUFFER_SIZE)
        )
        # Disable CIA #2 IRQs, clear ICR, then point NMI vector → $C020.
        self.api.write_regs(f"{CIA2.ICR:04X}", CIA2_ICR_DISABLE_ALL, CIA2_ICR_CLEAR)
        self.api.write_regs(
            f"{VECTORS.NMI:04X}", NMI_ROUTINE_ADDR & 0xFF, (NMI_ROUTINE_ADDR >> 8) & 0xFF
        )
        if self.digi_boost:
            self._enable_digi_boost()

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

    def _nmi_latch_value(self) -> int:
        """CIA #2 Timer A latch for the NMI DAC consumer at sample_rate.

        Timer A counts N→0 inclusive = N+1 PHI2 ticks per fire, so the NMI
        period is (latch+1) cycles. Pick the integer latch whose (latch+1)
        period brings the consumer rate closest to sample_rate. NTSC@8kHz:
        latch=127 (7990 Hz, -0.12%); PAL@8kHz: latch=122 (8010 Hz, +0.13%).
        The REU pump's CIA #1 latch and the servo's feed-forward both derive
        from this so the producer/consumer ratio stays exact.
        """
        clock = CLOCK_NTSC if self.system == "NTSC" else CLOCK_PAL
        return max(1, round(clock / self.sample_rate) - 1)

    def _start_nmi_timer(self) -> None:
        # The NMI always runs at the nominal sample-rate latch. Pitch
        # compensation is done host-side by the resampler (see
        # set_pitch_compensation_for_mode), not by speeding the NMI up — so the
        # consumer rate stays clear of the firmware DMA/badline wedge regime and
        # the REU pump's CIA #1 derivation (off _nmi_latch_value) stays exact.
        latch = self._nmi_latch_value()
        self._nmi_latch = latch
        self.api.write_regs(f"{CIA2.TIMER_A_LO:04X}", latch & 0xFF, (latch >> 8) & 0xFF)
        # Arm + start timer A, set NMI source.
        self.api.write_regs(f"{CIA2.ICR:04X}", CIA2_ICR_ENABLE_TIMER_A_NMI, CIA2_TIMER_A_CONTINUOUS)

    def set_pitch_compensation_for_mode(
        self, display_mode: str, calibration: dict[str, float] | None = None
    ) -> None:
        """Set the host-side resample ratio for a display mode to restore pitch.

        The host-DMA servo locks audio playback speed to the NMI consumer R,
        which loses ~1-14% of its ticks to video DMA bus-halts (heavier video =
        more halts = slower R), so playback comes out a touch slow. ``calibration``
        maps a display-mode name to a **playback-rate multiplier** (from
        ``[audio] pitch_mult_*``): >1.0 means "play this much faster to cancel the
        slowdown." We convert it to a fixed decimation ratio = 1 / multiplier and
        hand it to the resampler (``_resample_residual``): dropping that fraction
        of source samples makes the fixed NMI consumption cover more source
        seconds, i.e. plays the mode `multiplier`× faster. Call at scene setup
        when the display mode changes.

        This is a *fixed* per-mode dial set once per scene — it does NOT chase the
        measured read-pointer R (which reads biased-low under bus load and made the
        old R-driven resampler over-correct ~10% fast). It is ears-tunable per mode
        via the pitch_mult_* config; it cannot track content-dependent DMA load, so
        it is a best-fit compromise the user dials, not a closed loop.

        Only applies under the host-DMA servo with a running worker; the REU pump
        pre-encodes the whole track offline (its own governor handles drift) and
        open-loop needs no adjustment.
        """
        if not self.host_dma_servo or not self._worker_thread:
            # REU pump has its own governor; open-loop doesn't need adjustment.
            return

        # `hires_edges` scenes report display_mode.name == "hires" (same VIC
        # fetch), so they already resolve to the `hires` multiplier here.
        multiplier = 1.0 if calibration is None else calibration.get(display_mode.lower(), 1.0)
        # ratio = 1/mult, clamped to (RESAMPLE_RATIO_MIN, 1.0]. mult < 1.0 (an
        # unusual "play slower" request) would upsample; we don't slow playback
        # down, so it clamps to 1.0 (passthrough). _resample_ratio is a plain
        # float, atomic to assign under the GIL — the producer reads it per chunk.
        ratio = max(RESAMPLE_RATIO_MIN, min(1.0, 1.0 / multiplier)) if multiplier > 0 else 1.0
        if ratio == self._resample_ratio:
            return
        log.debug(
            f"[audio] pitch comp for {display_mode}: resample ratio "
            f"{self._resample_ratio:.4f} → {ratio:.4f} (rate ×{multiplier:.4f})"
        )
        self._resample_ratio = ratio

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
        closed-loop `_next_pace_increment(...)` (a PI controller on the gap
        to R) instead of the bare `chunk_period`. This still adds to the
        *absolute* `next_write_time` — the no-snap-forward property above is
        preserved — but lets W's average rate track the (bus-halt-throttled)
        NMI consumer so the gap can't drift and lap (the ~26s echo). The
        increment is clamped to [0.5, 1.5]·chunk_period so a single bad
        reading can't stall or sprint the schedule."""
        try:
            write_addr = RING_BUFFER_ADDR
            prebuffered = False
            bytes_prebuffered = 0
            chunk_buf = bytearray(self.chunk_size)
            # A partially-consumed queue blob carried to the next chunk, as
            # (remaining_bytes, src_weight). The weight is credited to
            # _queued_samples (a SOURCE-sample tally) only when the blob's last
            # byte is consumed, so the resampler's byte≠source-sample ratio can't
            # drift the position clock. None = nothing carried.
            leftover: tuple[bytes, int] | None = None
            chunk_period = self.chunk_size / self.sample_rate
            prebuffer_bytes = PREBUFFER_CHUNKS * self.chunk_size
            # Pace + collect deadlines. Zero until NMI starts.
            next_write_time = 0.0

            while self.running:
                if prebuffered:
                    pace_deadline = next_write_time
                    collect_deadline = pace_deadline
                else:
                    pace_deadline = 0.0
                    collect_deadline = time.monotonic() + chunk_period

                n = 0
                src_from_queue = 0  # source samples whose blobs fully drained

                if leftover is not None:
                    lbytes, lweight = leftover
                    take = min(len(lbytes), self.chunk_size)
                    chunk_buf[:take] = lbytes[:take]
                    n = take
                    if take < len(lbytes):
                        leftover = (lbytes[take:], lweight)  # still partial; defer
                    else:
                        src_from_queue += lweight  # fully drained → credit weight
                        leftover = None

                # Block with deadline. Returns early once chunk_buf is
                # full or once the producer is silent past the deadline.
                while n < self.chunk_size and leftover is None and self.running:
                    remaining = collect_deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    try:
                        payload, weight = self.q.get(timeout=remaining)
                    except queue.Empty:
                        break
                    take = min(len(payload), self.chunk_size - n)
                    chunk_buf[n : n + take] = payload[:take]
                    n += take
                    if take < len(payload):
                        leftover = (payload[take:], weight)  # partial; defer weight
                    else:
                        # Fully consumed (also covers a 0-byte decimator-stash
                        # blob, which credits its source weight immediately).
                        src_from_queue += weight

                if not self.running:
                    break

                if n == 0:
                    if not prebuffered:
                        # Idle: no producer data, no NMI to feed.
                        continue
                    # Real underrun: refresh ring with silence.
                    chunk_buf[:] = bytes([NEUTRAL_SAMPLE] * self.chunk_size)
                    n = self.chunk_size
                    self._full_underruns += 1
                elif n < self.chunk_size and prebuffered:
                    # Partial chunk: pad to keep pace math simple. Pad bytes
                    # carry no source weight (not from the queue).
                    pad = self.chunk_size - n
                    chunk_buf[n : n + pad] = bytes([NEUTRAL_SAMPLE]) * pad
                    n = self.chunk_size
                    self._partial_underruns += 1

                if prebuffered:
                    sleep_s = pace_deadline - time.monotonic()
                    if sleep_s > 0:
                        time.sleep(sleep_s)

                self.api.write_memory_file(f"{write_addr:04X}", bytes(chunk_buf[:n]))
                if src_from_queue:
                    self._queued_samples = max(0, self._queued_samples - src_from_queue)
                write_addr += n
                if write_addr >= RING_BUFFER_END:
                    write_addr = RING_BUFFER_ADDR

                if not prebuffered:
                    bytes_prebuffered += n
                    if bytes_prebuffered >= prebuffer_bytes:
                        self._start_nmi_timer()
                        prebuffered = True
                        # R only becomes meaningful now that the NMI consumes;
                        # start the servo integrator clean.
                        self._servo_integ = 0.0
                        # Pace the next write one chunk_period out so the
                        # PREBUFFER_CHUNKS slack stays steady instead of
                        # getting eaten up immediately.
                        next_write_time = time.monotonic() + chunk_period
                else:
                    next_write_time += self._next_pace_increment(write_addr, chunk_period)
        except Exception:
            # Without this, a thread crash means audio goes silent forever and
            # main loop has no clue why. Mark not-running so callers can detect.
            log.exception("audio worker crashed")
            self.running = False

    def _next_pace_increment(self, write_addr: int, chunk_period: float) -> float:
        """Per-chunk pace increment for the prebuffered worker.

        Open-loop (host_dma_servo off, or a failed/insane R read) returns the
        bare ``chunk_period`` — the original strict wall-clock schedule. With the
        servo on, reads the NMI read pointer R over REST, computes the ring gap
        ``(write_addr - R) % RING_BUFFER_SIZE`` (write_addr is the live W head —
        already advanced past the byte just written), and runs the PI controller
        (``_servo_period``) so W's pace tracks R and the gap locks near half a
        ring instead of lapping. A flaky read degrades to open-loop for that one
        chunk; it never crashes or freezes the schedule. The increment is added
        to the *absolute* ``next_write_time`` by the caller, so REST read latency
        only shortens the next sleep — it does not snap the schedule forward.
        """
        if not self.host_dma_servo:
            return chunk_period
        r = self.api.read_memory(READ_PTR_LO_ADDR, 2)
        if r is None or len(r) != 2:
            return chunk_period
        r_addr = r[0] | (r[1] << 8)
        if not (RING_BUFFER_ADDR <= r_addr < RING_BUFFER_END):
            return chunk_period
        gap = (write_addr - r_addr) % RING_BUFFER_SIZE
        self._servo_gap_last = gap
        self._servo_gap_min = gap if self._servo_gap_min < 0 else min(self._servo_gap_min, gap)
        self._servo_gap_max = max(self._servo_gap_max, gap)
        period, self._servo_integ = _servo_period(gap, self._servo_integ, chunk_period=chunk_period)
        return period

    # ---- sample tap ----------------------------------------------------------
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

    # ---- fixed-ratio resampler -----------------------------------------------
    def _resample_residual(self, floats: np.ndarray) -> np.ndarray:
        """Decimate float samples by the fixed per-mode ``_resample_ratio`` so a
        bus-halt-slowed display mode plays back at correct tempo/pitch. A ratio
        < 1.0 drops that fraction of samples (playback runs 1/ratio× faster);
        ratio within RESAMPLE_DEADBAND of 1.0 (the default for light modes) is a
        bit-exact passthrough.

        Linear interpolation with a fractional phase accumulator carried across
        chunks (``_resample_phase``) plus the previous chunk's last sample
        (``_resample_prev_tail``) so the decimation is seam-free — concatenating
        the per-chunk outputs equals a one-shot resample of the concatenated
        input. The seam state is owned by this (single producer) thread; the ratio
        is a fixed scalar set at scene setup, NOT chased from the read pointer R.
        numpy-only (no scipy runtime dep)."""
        ratio = self._resample_ratio
        if floats.size == 0 or ratio >= 1.0 - RESAMPLE_DEADBAND:
            # Passthrough. Drop any stale seam so a later decimating session
            # (different scene) starts its phase clean instead of from a tail.
            self._resample_phase = 0.0
            if self._resample_prev_tail.size:
                self._resample_prev_tail = np.zeros(0, dtype=np.float32)
            return floats
        ratio = max(RESAMPLE_RATIO_MIN, ratio)  # sanity floor on the decimation
        floats = floats.astype(np.float32, copy=False)
        x = (
            np.concatenate((self._resample_prev_tail, floats))
            if self._resample_prev_tail.size
            else floats
        )
        if x.size < 2:
            # Can't interpolate across <2 samples; stash and emit nothing. Phase
            # stays valid (x[0] is unchanged as the next chunk's index 0).
            self._resample_prev_tail = x
            return np.zeros(0, dtype=np.float32)
        inv = 1.0 / ratio  # source samples advanced per output sample (> 1.0)
        p0 = self._resample_phase
        last = x.size - 1
        # Count outputs whose source position pos = p0 + inv*k stays <= the last
        # interpolable index. The next chunk's index 0 is x[-1], so carry the
        # phase relative to that sample.
        m = int(np.floor((last - p0) / inv)) + 1
        if m <= 0:
            self._resample_phase = p0 - last
            self._resample_prev_tail = x[-1:]
            return np.zeros(0, dtype=np.float32)
        pos = p0 + inv * np.arange(m, dtype=np.float64)
        idx = np.minimum(np.floor(pos).astype(np.int64), last - 1)
        frac = (pos - idx).astype(np.float32)
        out: np.ndarray = x[idx] * (1.0 - frac) + x[idx + 1] * frac
        self._resample_phase = (p0 + inv * m) - last
        self._resample_prev_tail = x[-1:]
        return out.astype(np.float32, copy=False)

    def _reset_resample_state(self) -> None:
        """Clear the producer-owned resampler seam. Called at session start +
        stop() (the producer thread is not running then), so the cross-chunk
        phase/prev-tail never carry over from a prior scene's stream."""
        self._resample_phase = 0.0
        self._resample_prev_tail = np.zeros(0, dtype=np.float32)

    # ---- shared encode + enqueue ---------------------------------------------
    def _encode_and_enqueue(self, floats: np.ndarray, block_on_full: bool = False) -> int:
        """Push float samples in [-1, 1] through the FFT tap and into the
        DAC queue as 4-bit values. Returns the number of SOURCE samples enqueued.

        Encodes the whole input array to one bytes blob and enqueues it in
        a single put. The previous per-sample loop hit ~88K lock
        acquisitions/sec on a 44.1 kHz PyAV stream; this is one per
        producer call (~10-40/sec).

        block_on_full: if True, block up to 200ms for queue capacity (used
        by the PyAV push path so the demuxer naturally throttles). If
        False, drop the whole blob when full (mic path, where the
        sounddevice callback is real-time and can't block). Backpressure
        is counted in samples (not blobs) against self._max_queued_samples.

        Counter-split for the fixed-ratio resampler: _pushed_count / _queued_samples
        and the queue blob's weight are all in SOURCE samples (n_src), so
        position_seconds (the A/V master clock) + the demuxer backpressure stay on
        the source timeline regardless of decimation; only the encoded PAYLOAD
        shrinks. The two are 1:1 with bytes when the resampler is a no-op."""
        if floats.size == 0:
            return 0
        floats = self._apply_dsp(floats)
        # Source-sample count drives all accounting (the A/V master clock); the
        # tap shows source-rate audio (spectrally ~identical to the decimated
        # output). Resample to the compensated rate AFTER both.
        n_src = int(floats.size)
        self._push_to_tap(floats.astype(np.float32, copy=False))
        resampled = self._resample_residual(floats)
        vol = encode_floats_to_dac(resampled, dither=self.dither_enabled)
        payload = vol.tobytes()
        # Sample-count backpressure (in SOURCE samples). Reading _queued_samples
        # without the GIL is racy with the worker decrement, but the worst case
        # is putting one blob over the cap — harmless given the soft ceiling.
        if self._queued_samples + n_src > self._max_queued_samples:
            if not block_on_full:
                return 0
            deadline = time.time() + QUEUE_PUT_TIMEOUT_S
            while self._queued_samples + n_src > self._max_queued_samples and self.running:
                if time.time() >= deadline:
                    return 0
                time.sleep(BACKPRESSURE_SPIN_S)
        try:
            if block_on_full:
                self.q.put((payload, n_src), timeout=QUEUE_PUT_TIMEOUT_S)
            else:
                self.q.put_nowait((payload, n_src))
        except queue.Full:
            return 0
        self._queued_samples += n_src
        self._pushed_count += n_src
        return n_src

    # ---- input sources -------------------------------------------------------
    def _mic_callback(self, indata: np.ndarray, frames: int, time_info: Any, status: Any) -> None:
        if status or not self.running:
            return
        mono = indata.mean(axis=1) if indata.ndim > 1 else indata[:, 0]
        mono = mono * self.sensitivity
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
        if not self._dsp_active():
            mono[np.abs(mono) < self.noise_gate] = 0
        mono = self._apply_dsp(mono.astype(np.float32, copy=False))
        self._push_to_tap(mono)
        vol = encode_floats_to_dac(mono, dither=self.dither_enabled)
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
        device: int,
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
        if self.use_reu_pump:
            self._start_mic_for_reu_pump(device, skip_irq_vector_hook=skip_irq_vector_hook)
            return
        self._upload_nmi_and_buffers()
        self._pushed_count = 0
        self._reset_resample_state()
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

    # ---- REU-staged mic (live capture, opt-in via use_reu_pump) -------------
    def _start_mic_for_reu_pump(self, device: int, *, skip_irq_vector_hook: bool = False) -> None:
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
        # 1. Pre-fill the REU mic ring with NEUTRAL so the pump's first
        # ~ring-size worth of reads play silence (not stale FPGA SRAM,
        # which could be loud noise). One REUWRITE slice = 32 KB, so two
        # slices cover the 64 KB ring.
        log.info(
            "audio[reu mic]: prefilling REU ring at $%06X (%d bytes)", REU_MIC_BASE, REU_MIC_SIZE
        )
        pad = bytes([NEUTRAL_SAMPLE] * REU_UPLOAD_SLICE)
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
        # $DF06 read-back garbage — see REU_MIC_SRC_TRACKER_ADDR comment).
        # Init REU regs: dest = RING_BUFFER_ADDR (start of main audio ring),
        # length = REU_PUMP_CHUNK_SIZE, address-control = 0 (both auto-inc,
        # no autoload). The src registers don't need init since the handler
        # writes them on every trigger.
        self.api.write_memory_file(f"{REU_PUMP_HANDLER_ADDR:04X}", REU_MIC_IRQ_HANDLER)
        self.api.write_memory(
            f"{REU_MIC_SRC_TRACKER_ADDR:04X}",
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
        self._start_nmi_timer()
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
        # sample_rate (200 ms at 8 kHz).
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

    def _resolve_input_device(self, device: int) -> tuple[int | None, str]:
        """Pick an input-capable device.

        - `device < 0`: use the system default input device (PortAudio
          accepts `None` for that).
        - The configured device exists and has input channels: use it.
        - Otherwise (output-only or unknown): fall back to the system
          default and warn the user that the configured device is unusable.

        Returns (device_or_None, friendly_name).
        """
        assert sd is not None

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

    def _open_input_stream(self, device: int, callback: Any = None) -> Any:
        """Open an InputStream with sensible channel-count fallback.

        CoreAudio (and a few ALSA drivers) reject `channels=1` on devices
        that internally only present stereo, with the generic PortAudio
        error code -9998 "Invalid number of channels". Try 1 first (most
        mics want it); fall back to the device's native channel count;
        finally try a few common counts before giving up with a useful
        error that lists alternative input devices.

        `callback` defaults to the host-DMA `_mic_callback`. The REU mic
        path passes `_mic_callback_reu` to redirect samples into the REU
        ring instead of the worker queue.
        """
        assert sd is not None
        if callback is None:
            callback = self._mic_callback
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
                f"Run `python -m c64cast -L` to list devices "
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
                    device=resolved, samplerate=self.sample_rate, channels=ch, callback=callback
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
                    self.sample_rate,
                    e,
                )
        raise RuntimeError(
            f"could not open mic on {dev_name!r} at "
            f"{self.sample_rate} Hz (tried channels {candidates}): "
            f"{last_err}"
        )

    # ---- external-source mode (used by PyAV demuxer) ------------------------
    def start_for_external_source(self) -> None:
        """Bring up NMI + worker without an input thread. Caller feeds samples
        via push_samples()."""
        self._upload_nmi_and_buffers()
        self._pushed_count = 0
        self._reset_resample_state()
        self.running = True
        self._worker_thread = threading.Thread(
            target=self._worker, daemon=True, name="audio-worker"
        )
        self._worker_thread.start()
        log.info("audio: external push source → SID @ %dHz", self.sample_rate)

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
        below 8 kHz due to bus halts, a smaller chunk keeps the ring from
        overflowing. See REU_PUMP_CHUNK_SIZE_HEAVY_BUS for the measured
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
        chunk = REU_PUMP_CHUNK_SIZE if chunk_size is None else chunk_size
        # CIA #1 latch: pump period = chunk × NMI period. The NMI period is
        # (NMI latch + 1) cycles — derive it from the actual consumer latch
        # rather than hardcoding 128, so a non-default sample_rate still gets a
        # matched pump rate. (At 8 kHz this is the historical chunk × 128 - 1.)
        nmi_period = self._nmi_latch_value() + 1
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
        eof_pad_bytes = self.sample_rate * 5
        log.info(
            "audio: REU upload %d bytes (%.1fs of source) + %d bytes EOF pad",
            len(audio_4bit),
            len(audio_4bit) / self.sample_rate,
            eof_pad_bytes,
        )
        t0 = time.perf_counter()
        for off in range(0, len(audio_4bit), REU_UPLOAD_SLICE):
            self.api.reu_write(REU_AUDIO_BASE + off, audio_4bit[off : off + REU_UPLOAD_SLICE])
        # EOF pad: write NEUTRAL_SAMPLE for the tail so the pump's read-past-
        # end-of-source plays silence instead of garbage.
        pad_payload = bytes([NEUTRAL_SAMPLE] * REU_UPLOAD_SLICE)
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
            prefill = prefill + bytes([NEUTRAL_SAMPLE] * (RING_BUFFER_SIZE - len(prefill)))
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
        self._start_nmi_timer()

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
        self._encode_and_enqueue(floats, block_on_full=True)

    def position_seconds(self) -> float:
        """Approximate playback position from the consumer's perspective.

        Host-DMA mode: (samples pushed - samples still queued) / sample_rate.
        REU pump mode: wall-clock seconds since the IRQ pump armed (clamped
        to the total source length so over-runs don't desync video). The C64
        ring buffer adds another ~0.5s of latency past either path, but
        that bias is constant in steady state and therefore harmless for
        relative sync.
        """
        if not self.sample_rate:
            return 0.0
        if self._reu_pump_armed:
            elapsed = time.monotonic() - self._reu_pump_start_time
            total_s = self._reu_pump_total_samples / self.sample_rate
            return max(0.0, min(elapsed, total_s))
        # q.qsize() now counts bytes-blobs, not samples — read the explicit
        # sample-count counter instead.
        consumed = self._pushed_count - self._queued_samples
        return max(0.0, consumed / self.sample_rate)

    def reset_position(self) -> None:
        self._pushed_count = 0

    # ---- shutdown ------------------------------------------------------------
    def stop(self) -> None:
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
            self.api.write_regs(f"{CIA2.ICR:04X}", CIA2_ICR_DISABLE_ALL, CIA2_ICR_CLEAR)
            self.api.write_memory("D418", "00")
            if self.digi_boost:
                self._disable_digi_boost()
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
        while not self.q.empty():
            try:
                self.q.get_nowait()
            except queue.Empty:
                break
        self._pushed_count = 0
        self._queued_samples = 0
        # Clear pitch-comp state so the next scene's bring-up starts from a no-op
        # (a scene with no display_mode never calls set_pitch_compensation_for_
        # mode, so a stale ratio must not leak across scenes).
        self._resample_ratio = 1.0
        self._reset_resample_state()
        # Report underrun telemetry. Each full underrun is an audible
        # click; partials are less audible but still indicate producer
        # stalls. Deterministic, source-correlated counts (same numbers
        # across reruns of the same video) point at PyAV decode
        # stalls rather than DMA timing.
        if self._full_underruns or self._partial_underruns:
            log.warning(
                "audio: %d full + %d partial underruns this session "
                "(producer stalled past pace deadline)",
                self._full_underruns,
                self._partial_underruns,
            )
        else:
            log.info("audio: clean session (no underruns)")
        self._full_underruns = 0
        self._partial_underruns = 0
        # Host-DMA servo gap telemetry: confirms the closed loop locked the
        # ring gap near half a ring (4096) and never approached a lap (0) or an
        # underrun (RING_BUFFER_SIZE). The external drift probe can't see this
        # (it assumes a fixed wall-clock W), so this is the non-ears check.
        if self._servo_gap_last >= 0:
            log.info(
                "audio: host-DMA servo gap last=%d min=%d max=%d (target=%d, lap at 0/%d)",
                self._servo_gap_last,
                self._servo_gap_min,
                self._servo_gap_max,
                HOST_DMA_SERVO_TARGET_GAP,
                RING_BUFFER_SIZE,
            )
        self._servo_gap_min = -1
        self._servo_gap_max = -1
        self._servo_gap_last = -1

    def close(self) -> None:
        # AudioStreamer doesn't own its API — it shares the render path's
        # C64Backend (single-connection DMA constraint). The caller closes
        # the API after the final reset; closing it here would strand reset().
        self.stop()
