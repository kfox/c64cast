"""The C64-side IRQ-handler layer for tear-free double-buffered video.

The 6502 machine code modes.py's bitmap modes upload and drive per frame:
the $C500 bank-swap raster IRQ handlers (hires, mhires, the chunked
mhires + REU-audio merged dispatcher, and the host-DMA page-flip sibling
for no-REU backends), the $C700 frame-tracker layouts each handler reads
at vblank, the REU staging addresses near 14 MB, and the bring-up /
teardown plus per-frame push helpers that stage a frame and arm the
tracker. Pure Python over C64Backend — no numpy, no cv2 — so the whole
module runs under mypy --strict.

Nothing here decides WHEN a pipeline engages: that's scene_factory's
resolve_use_reu_staged / resolve_double_buffer, and the DisplayMode
classes in modes.py own the per-frame compose + call order. See
docs/architecture/video-color.md ("[video].use_reu_staged" and
"[video].double_buffer") for the design and hardware history behind
these bytes.
"""

from __future__ import annotations

import logging

from c64cast.audio.audio_handlers import REU_PUMP_BODY_SUBROUTINE_ADDR
from c64cast.hw.backend import C64Backend
from c64cast.hw.c64 import (
    CIA1,
    CIA2,
    KERNAL,
    RASTER_COMMIT_LAST_SAFE_LINE,
    RASTER_VBLANK_LINE,
    REU,
    SCREEN,
    VECTORS,
    VIC_BANK_0,
    VIC_BANK_2,
)

log = logging.getLogger(__name__)


# --- REU-staged video pipeline (experimental, opt-in) --------------------
# Selected char-mode display modes can render their per-frame screen RAM by
# first pushing the 1000 bytes to REU SRAM via socket DMA opcode 0xFF07
# (REUWRITE — bus-clean, no SID perturbation), then triggering a REU→main
# DMA on the C64 to drop the screen bytes into VIC's screen-RAM area in one
# shot. Color RAM ($D800) is never banked and stays on the regular
# DMAWRITE path. The opt-in flag flows from `[video].use_reu_staged` in
# TOML to the display mode constructor.
#
# Slice 1 (current): single-buffer — REU→main writes into the
# currently-displayed $0400. The screen RAM gets stomped during the
# transfer, but the visible artifact is one frame's worth at most. No bank
# swap yet (the bank-swap bytes are defined in c64.VIC_BANK_0 / VIC_BANK_2
# / CIA2.PORT_A_BANK_* and ready for a future slice that pairs the REU
# trigger with a $DD00 swap via a C64-side handler).
#
# Coexistence with REU audio: this path drives the REU controller's REC
# registers from the host, while the REU audio pump (audio.start_for_reu_staged)
# drives them from a kernal-IRQ handler on the C64. They share one set of
# registers — if both are active, REU writes will interleave unpredictably
# and audio will glitch or stop. Mutual exclusion is enforced at scene
# setup; the resulting useful pairing today is REU video + host-DMA audio
# (e.g. mic on a webcam scene) or REU video + no audio.
REU_VIDEO_SCREEN_BASE = 0xE00000  # 14 MB in — way past any REU audio region
REU_VIDEO_SCREEN_LEN = SCREEN.N_CELLS  # 1000 bytes of PETSCII screen codes

# --- REU-staged bitmap pipeline (double-buffer, bank-swap) ---------------
# HiresDisplayMode opt-in path. Each frame is REUWRITE-staged into REU
# SRAM (bus-clean), then a pair of REU→main DMAs drop the bitmap + screen
# into the OFF-SCREEN VIC bank's addresses while the on-screen bank keeps
# being rendered (no visible tearing during the transfer). A C64-side
# raster IRQ at line $F8 reads a pending-bank byte in main RAM and, when
# set, writes the new $DD00 value to flip which bank VIC fetches from —
# a 1-cycle, vblank-aligned swap. The host alternates target_bank between
# 0 (bank 0 @ $2000/$0400) and 1 (bank 2 @ $A000/$8400) each frame.
#
# Memory map (both banks always reserved while this path is active):
#   Bank 0: bitmap $2000-$3F3F, screen $0400-$07E7
#   Bank 2: bitmap $A000-$BF3F, screen $8400-$87E7
#   Bank 1 unchanged: audio ring at $4000-$5FFF
#   Color RAM at $D800 unused by hires (color encoded in screen RAM nibbles).
#
# REU staging layout (reused each frame; the DMA dest changes per target_bank):
#   $E10000-$E11F3F  bitmap staging (8000 bytes)
#   $E12000-$E123E7  screen staging (1000 bytes)
#
# Coexistence: shares the REC controller with the REU audio pump. Mutex
# is enforced at validate_scene_cfg — REU video on a hires scene cannot
# coexist with REU audio (mic on webcam OR video pre-encode), because
# both arm IRQ handlers via $0314.
REU_VIDEO_BITMAP_BASE = 0xE10000
REU_VIDEO_BITMAP_LEN = SCREEN.BITMAP_BYTES  # 8000 bytes
REU_VIDEO_BITMAP_SCREEN_BASE = 0xE12000  # 1000-byte screen for hires
REU_VIDEO_BITMAP_SCREEN_LEN = SCREEN.N_CELLS
# MultiHires adds per-cell color RAM ($D800) on top of bitmap+screen. Color
# RAM isn't VIC-banked — one shared SRAM is read by VIC regardless of which
# bank is currently displayed — so the IRQ handler triggers a third REU→main
# DMA into $D800 right before the bank swap. The DMA is fast enough (~1000
# cycles ≈ 16 raster lines) that the c3-mismatch window across the bank
# swap is bounded to one VIC cell row at most; on stationary content it's
# imperceptible, on motion content it's a 1-row band of "wrong c3" at the
# tear line that the eye reads as part of the bank-swap location anyway.
REU_VIDEO_BITMAP_COLOR_BASE = 0xE13000  # 1000-byte color RAM staging
REU_VIDEO_BITMAP_COLOR_LEN = SCREEN.N_CELLS

# C64-side bank-swap raster IRQ handler. Lives at $C500 (audio_handlers.py owns
# $C000-$C2FF for NMI DAC + REU pump handlers; api.py uses $C300/$C400
# for the SID player + re-INIT stub; big_text.py uses $C000 — but
# big_text is only valid on `blank`/`mcm` scenes, and HiresDisplayMode
# is a bitmap mode, so they never coexist). The frame tracker at
# $C700-$C70F holds everything the IRQ needs per frame, packed
# contiguously so the host can stage a frame in one DMAWRITE.
BANK_SWAP_IRQ_HANDLER_ADDR = 0xC500
FRAME_TRACKER_ADDR = 0xC700

# Frame tracker layout (16 bytes at $C700-$C70F). The host packs this
# in a single 16-byte DMAWRITE per frame — the wire FIFO guarantees
# either all-new or all-old contents on the C64 side, so the IRQ never
# sees half-updated regs paired with a fresh ready flag.
#
#   $C700-$C706 : bitmap REU regs ($DF02-$DF08 pre-staged values, 7 bytes)
#                 c64_lo, c64_hi, reu_lo, reu_mi, reu_hi, len_lo, len_hi
#   $C707-$C70D : screen REU regs (same layout, 7 bytes)
#   $C70E       : pending bank value ($97 = bank 0, $95 = bank 2)
#   $C70F       : ready flag (1 = frame staged, 0 = no new frame)
#
# IRQ handler clears $C70F after committing; host sets $C70F = 1 (last
# byte of the DMAWRITE blob) to arm. A skipped IRQ (ready=0) just chains
# straight to kernal — costs ~13 cycles, negligible.
FRAME_TRACKER_LEN = 16
TRACKER_OFF_BITMAP_REGS = 0  # 7 bytes
TRACKER_OFF_SCREEN_REGS = 7  # 7 bytes
TRACKER_OFF_BANK_VALUE = 14  # 1 byte
TRACKER_OFF_READY_FLAG = 15  # 1 byte

# C64-side raster IRQ handler. On every IRQ at line 248 (vblank):
#   * AND $D019 with $01 — isolate raster source bit. If 0, chain.
#   * Ack raster IRQ ($D019 = $01, write-1-to-clear).
#   * Read $C70F ready flag. If 0, chain (no new frame to swap in).
#   * Copy $C700-$C706 → $DF02-$DF08 (bitmap REU regs).
#   * Trigger bitmap DMA ($DF01 = $91). CPU halts ~8000 cycles while
#     REU→main copies into the off-screen bitmap addr. VIC continues
#     fetching the visible bank (bank 0 or bank 2, whichever was last
#     swapped to). NMI is blocked during the halt — same as host-DMAWRITE.
#   * Copy $C707-$C70D → $DF02-$DF08 (screen REU regs).
#   * Trigger screen DMA ($DF01 = $91). CPU halts ~1000 cycles.
#   * Load $C70E (bank value) and store to $DD00 — 1-cycle swap that
#     flips VIC to the just-painted bank during vblank (tear-free).
#   * Clear $C70F so the next IRQ skips until the host stages a new frame.
#   * Chain to kernal $EA31 for SCNKEY / UDTIM / cursor blink.
#
# Why have the IRQ trigger the DMA instead of the host? Doing it host-
# side adds Python-paced jitter to a sequence that's otherwise deterministic
# (kernal IRQ fires on a clockwork CIA #1 timer). The earlier reu_irq_pump
# experiment ([u64_reu_socket_dma.md] Phase 2 v2) found that deterministic
# C64-side IRQ-paced REU DMAs sounded perceptually cleaner than jittery
# host-paced ones, even when measured sideband power was the same or higher.
# Moving the trigger here also collapses 6 host socket round-trips per
# frame (2× setup + 2× trigger + pending flag) into 1 (the 16-byte tracker
# DMAWRITE), and eliminates host-induced mid-frame bus halts entirely.
#
# A/X/Y survive: kernal $FF48 saved A/X/Y before vectoring through
# $0314; our handler uses A + X, both of which $EA81 restores via PLA.
# Same convention as big_text.py's raster IRQ ([overlays/big_text.py:104])
# and the REU pump ([audio_handlers.py REU_IRQ_HANDLER]).
#
# Offsets must be exact: BEQ at offset 5 (+51 → 58), BEQ at offset 13
# (+43 → 58), BPL at offsets 24 + 40 (-9 → 17 + 33). The assert below
# catches length drift; if you edit the bytes, recompute all four branches.
BANK_SWAP_IRQ_HANDLER = bytes(
    [
        0xAD,
        0x19,
        0xD0,  # 0  LDA $D019         ; VIC IRQ status
        0x29,
        0x01,  # 3  AND #$01          ; raster bit
        0xF0,
        0x33,  # 5  BEQ +51 → 58      ; not raster → chain
        0x8D,
        0x19,
        0xD0,  # 7  STA $D019         ; ack raster
        0xAD,
        0x0F,
        0xC7,  # 10 LDA $C70F         ; ready flag
        0xF0,
        0x2B,  # 13 BEQ +43 → 58      ; no frame staged → chain
        0xA2,
        0x06,  # 15 LDX #$06
        0xBD,
        0x00,
        0xC7,  # 17 LDA $C700,X       ; copy bitmap regs
        0x9D,
        0x02,
        0xDF,  # 20 STA $DF02,X
        0xCA,  # 23 DEX
        0x10,
        0xF7,  # 24 BPL -9 → 17       ; loop over 7 bytes
        0xA9,
        0x91,  # 26 LDA #$91
        0x8D,
        0x01,
        0xDF,  # 28 STA $DF01         ; trigger bitmap DMA (~8000 cyc halt)
        0xA2,
        0x06,  # 31 LDX #$06
        0xBD,
        0x07,
        0xC7,  # 33 LDA $C707,X       ; copy screen regs
        0x9D,
        0x02,
        0xDF,  # 36 STA $DF02,X
        0xCA,  # 39 DEX
        0x10,
        0xF7,  # 40 BPL -9 → 33       ; loop
        0xA9,
        0x91,  # 42 LDA #$91
        0x8D,
        0x01,
        0xDF,  # 44 STA $DF01         ; trigger screen DMA (~1000 cyc halt)
        0xAD,
        0x0E,
        0xC7,  # 47 LDA $C70E         ; bank value
        0x8D,
        0x00,
        0xDD,  # 50 STA $DD00         ; swap (1 cycle)
        0xA9,
        0x00,  # 53 LDA #$00
        0x8D,
        0x0F,
        0xC7,  # 55 STA $C70F         ; clear ready flag
        0x4C,
        0x31,
        0xEA,  # 58 JMP $EA31         ; chain to kernal
    ]
)
assert len(BANK_SWAP_IRQ_HANDLER) == 61, (
    "BANK_SWAP_IRQ_HANDLER length changed — the 4 branch offsets (+51 and "
    "+43 forward, -9 twice for the loops) must be recomputed before "
    "changing. See the offsets in the byte-comment column."
)


# --- MultiHires bank-swap IRQ handler --------------------------------------
# Extends the hires handler: same bitmap + screen REU→main DMAs, but adds a
# third DMA into shared $D800 color RAM and a $D021 bg0 register write
# before the bank swap. The DMA order matters — see the long comment block
# on MHIRES_FRAME_TRACKER below for why color goes BEFORE the swap, not
# before bitmap/screen (TL;DR: color RAM is read by VIC regardless of bank,
# so updating it before the swap minimizes the bitmap-vs-color mismatch
# window during the visible frame).
#
# Tracker layout extends to 24 bytes at $C700-$C717:
#   $C700-$C706 : bitmap REU regs    ($DF02-$DF08 staged values)
#   $C707-$C70D : screen REU regs
#   $C70E-$C714 : color REU regs (NEW; dest = $D800, len = 1000)
#   $C715       : bg0 value to write to $D021 (NEW)
#   $C716       : pending bank value ($97 = bank 0, $95 = bank 2)
#   $C717       : ready flag (1 = frame staged) — moved from hires's $C70F
#
# The hires and mhires handlers share BANK_SWAP_IRQ_HANDLER_ADDR ($C500)
# and FRAME_TRACKER_ADDR ($C700) because they're mutually exclusive (a
# scene only has one display mode at a time). Each install function writes
# its own handler bytes + tracker length.
#
# Offsets must be exact: BEQ at offset 5 (+73 → 80), BEQ at offset 13
# (+65 → 80), BPL at offsets 24, 40, 56 (all -9 to their respective loop
# starts at offsets 17, 33, 49). The assert below catches length drift;
# if you edit the bytes, recompute all five branches.
MHIRES_BANK_SWAP_IRQ_HANDLER = bytes(
    [
        0xAD,
        0x19,
        0xD0,  # 0  LDA $D019         ; VIC IRQ status
        0x29,
        0x01,  # 3  AND #$01          ; raster bit
        0xF0,
        0x49,  # 5  BEQ +73 → 80      ; not raster → chain
        0x8D,
        0x19,
        0xD0,  # 7  STA $D019         ; ack raster
        0xAD,
        0x17,
        0xC7,  # 10 LDA $C717         ; ready flag
        0xF0,
        0x41,  # 13 BEQ +65 → 80      ; no frame staged → chain
        0xA2,
        0x06,  # 15 LDX #$06
        0xBD,
        0x00,
        0xC7,  # 17 LDA $C700,X       ; copy bitmap regs
        0x9D,
        0x02,
        0xDF,  # 20 STA $DF02,X
        0xCA,  # 23 DEX
        0x10,
        0xF7,  # 24 BPL -9 → 17       ; loop over 7 bytes
        0xA9,
        0x91,  # 26 LDA #$91
        0x8D,
        0x01,
        0xDF,  # 28 STA $DF01         ; trigger bitmap DMA (~8000 cyc halt)
        0xA2,
        0x06,  # 31 LDX #$06
        0xBD,
        0x07,
        0xC7,  # 33 LDA $C707,X       ; copy screen regs
        0x9D,
        0x02,
        0xDF,  # 36 STA $DF02,X
        0xCA,  # 39 DEX
        0x10,
        0xF7,  # 40 BPL -9 → 33       ; loop
        0xA9,
        0x91,  # 42 LDA #$91
        0x8D,
        0x01,
        0xDF,  # 44 STA $DF01         ; trigger screen DMA (~1000 cyc halt)
        0xA2,
        0x06,  # 47 LDX #$06
        0xBD,
        0x0E,
        0xC7,  # 49 LDA $C70E,X       ; copy color regs (NEW)
        0x9D,
        0x02,
        0xDF,  # 52 STA $DF02,X
        0xCA,  # 55 DEX
        0x10,
        0xF7,  # 56 BPL -9 → 49       ; loop
        0xA9,
        0x91,  # 58 LDA #$91
        0x8D,
        0x01,
        0xDF,  # 60 STA $DF01         ; trigger color DMA (~1000 cyc halt)
        0xAD,
        0x15,
        0xC7,  # 63 LDA $C715         ; bg0 value (NEW)
        0x8D,
        0x21,
        0xD0,  # 66 STA $D021         ; set bg0 ($D021)
        0xAD,
        0x16,
        0xC7,  # 69 LDA $C716         ; bank value
        0x8D,
        0x00,
        0xDD,  # 72 STA $DD00         ; swap (1 cycle)
        0xA9,
        0x00,  # 75 LDA #$00
        0x8D,
        0x17,
        0xC7,  # 77 STA $C717         ; clear ready flag
        0x4C,
        0x31,
        0xEA,  # 80 JMP $EA31         ; chain to kernal
    ]
)
assert len(MHIRES_BANK_SWAP_IRQ_HANDLER) == 83, (
    "MHIRES_BANK_SWAP_IRQ_HANDLER length changed — the 5 branch offsets "
    "(+73 and +65 forward, -9 three times for the loops) must be "
    "recomputed before changing. See the offsets in the byte-comment column."
)

# MultiHires tracker (24 bytes at $C700). Layout pairs 1:1 with the handler's
# hardcoded offsets above. The host packs this as a single 24-byte DMAWRITE
# per frame — the socket FIFO guarantees the C64 sees either all-new or
# all-old contents, so the IRQ can't catch ready=1 paired with stale regs.
MHIRES_FRAME_TRACKER_LEN = 24
MHIRES_TRACKER_OFF_BITMAP_REGS = 0  # 7 bytes
MHIRES_TRACKER_OFF_SCREEN_REGS = 7  # 7 bytes
MHIRES_TRACKER_OFF_COLOR_REGS = 14  # 7 bytes
MHIRES_TRACKER_OFF_BG0 = 21  # 1 byte
MHIRES_TRACKER_OFF_BANK_VALUE = 22  # 1 byte
MHIRES_TRACKER_OFF_READY_FLAG = 23  # 1 byte

# --- Merged dispatcher: bank-swap + audio REU pump fall-through ----------
# Today the bank-swap handler at $C500 chains to $EA31 on non-raster IRQs
# (i.e. CIA #1 jiffy). When the scene ALSO opted into REU audio, the
# audio pump handler at $C100 (37 B video / 102 B mic) wants every
# CIA #1 IRQ to run its REU→ring drain. The two handlers can't both own
# $0314 — historically `validate_scene_cfg` rejected the combination.
#
# The merge lifts that restriction by appending `JMP $C100` to the bank-
# swap handler and retargeting its first BEQ ("not raster → chain") to
# fall through to that JMP instead of to the chain-to-kernal. The 6502
# can't preempt IRQ handlers (I flag), so audio + bank-swap serialize
# naturally — each fully completes its REC ($DF02-$DF08) use before
# returning. The audio handler at $C100 stays byte-for-byte identical
# (audio_handlers.py owns its bytes; this side only routes execution there).
AUDIO_HANDLER_INSTALL_ADDR = 0xC100  # where audio.AudioStreamer uploads its REU pump
AUDIO_HANDLER_STUB = bytes([0x4C, 0x31, 0xEA])  # JMP $EA31


def _make_merged_handler(base: bytes, audio_jmp_target: int = AUDIO_HANDLER_INSTALL_ADDR) -> bytes:
    """Derive a merged dispatcher from a base bank-swap handler.

    The dispatcher replaces the base handler's trailing `JMP $EA31`
    (chain-to-kernal) with a JMP $EA31 chain followed by a JMP $C100
    audio handler fallthrough target for the non-raster path. The
    first BEQ at offset 5 (non-raster → audio) is retargeted from the
    chain to the audio JMP.

    Layout (offsets relative to base — extension replaces base[-3:]):
        body = base[:-3]
        extension at body_len:
            JMP $EA31         ; +0..2   ; chain to kernal (raster path)
            JMP $C100         ; +3..5   ; audio handler entry (non-raster)

    Empirical history (2026-05-27, Cam Link envelope FFT on day-in-life
    mhires REU bank-swap + REU audio pump):
      * A prior variant inserted an LDA $DC0D / AND #$01 / BNE check
        between the chain and the audio fallthrough — intended to
        recover CIA #1 IRQs that the kernal's $DC0D-read might ACK
        on chain-back. Folded envelope showed 35 % peak-to-peak
        excursion at 30 Hz and 12 % AM depth at 60 Hz.
      * Stripping the check (this form) drops 60 Hz depth ~84 % and
        30 Hz excursion to ~25 %. The kernal-ACK loss is small enough
        in practice (bank-swap halt ~2 ms; only a fraction of CIA #1
        wraps land inside it) that the pump still matches NMI over
        the audio ring's 1-sec buffer.
      * C (REU audio without bank-swap) sits at 1.2 % excursion;
        bank-swap REC DMAs themselves still drive the residual 25 %
        in D and are not addressable without splitting the per-frame
        REC into smaller pieces.
    """
    body = bytes(base[:-3])
    extension = bytes(
        [
            0x4C,
            0x31,
            0xEA,  # +0  JMP $EA31 (chain)
            0x4C,
            audio_jmp_target & 0xFF,  # +3  JMP $C100 (audio fallthrough)
            (audio_jmp_target >> 8) & 0xFF,
        ]
    )
    merged = bytearray(body + extension)
    audio_jmp_offset = len(body) + 3
    new_displacement = audio_jmp_offset - 7
    if not 0 <= new_displacement < 128:
        raise ValueError(
            f"merged handler displacement {new_displacement} out of "
            f"single-byte BEQ range for base handler of {len(base)} bytes"
        )
    merged[6] = new_displacement
    return bytes(merged)


# Pre-built merged dispatchers. Hires base = 61 → merged = 61 - 3 + 13 = 71 B.
# Mhires base = 83 → merged = 83 - 3 + 13 = 93 B. These are installed at
# $C500 in place of the base handlers when the scene combines REU video
# bank-swap with REU audio pump.
BANK_SWAP_PLUS_AUDIO_IRQ_HANDLER = _make_merged_handler(BANK_SWAP_IRQ_HANDLER)
MHIRES_BANK_SWAP_PLUS_AUDIO_IRQ_HANDLER = _make_merged_handler(MHIRES_BANK_SWAP_IRQ_HANDLER)
assert len(BANK_SWAP_PLUS_AUDIO_IRQ_HANDLER) == 64
assert len(MHIRES_BANK_SWAP_PLUS_AUDIO_IRQ_HANDLER) == 86


# --- Chunked mhires merged dispatcher ------------------------------------
# The plain merged dispatcher above triggers one large REC DMA per family
# (bitmap = 8000 bytes ≈ 8 ms halt, screen = 1000 ≈ 1 ms, color = 1000 ≈
# 1 ms). NMI fires at 8 kHz = every 125 cycles (≈ 125 µs at 1 MHz NTSC).
# CIA #2 is edge-triggered through the NMI line: when the bus halt covers
# multiple NMI underflows, the ICR bit latches once and the rest collapse
# into the same edge — losing every NMI past the first per halt.
#
# Empirically (2026-05-27 Cam Link D-vs-C diagnosis): the plain mhires
# merged dispatcher loses ~30 % of NMI events per frame, slowing 8 kHz
# playback to ~5 600 Hz effective. The music's BPM drops to ~70 % and
# the slow drift creates the "echo / time-stretch" the user reported.
#
# Fix: split each REC into 100-byte chunks. 100 bytes × 1 cyc/byte =
# 100 µs halt per chunk, comfortably under the 125 µs NMI period — so
# every NMI underflow lands either between chunks or in the active code
# right after a halt, and is serviced before the next underflow can
# collapse onto it. Bitmap: 80 chunks; screen + color: 10 chunks each.
# After each chunk DMA, only the LENGTH register decrements to 0; the
# src/dst registers auto-increment and stay valid across chunks, so the
# per-chunk inner body is just "reload length, retrigger" + the standard
# DEC/BNE counter.
#
# CIA #1 (audio pump) loss is partially addressed by per-family pump
# JSR calls (3 per bank-swap). After each family's chunk loop ends, the
# handler reads $DC0D / AND #$01 / BEQ skip / JSR $C180 — picking up any
# CIA #1 underflow that latched into the ICR during the family's halt
# time. Per-CHUNK pump checks would be ideal but break the bitmap's REC
# auto-increment (pump_body overwrites $DF02..$DF06 with the audio
# REU/main addresses, so the next bitmap chunk would re-trigger a
# 100-byte transfer from audio → audio rather than the next bitmap
# slice; the per-family check is safe because each family begins with
# its own copy-from-tracker loop that re-sets REC).
#
# Capture rate per ~33 ms bank-swap cycle: bitmap (10.7 ms halt, ~1.07
# underflows, 1 latched) + screen (1.34 ms, ~0.13) + color (1.34 ms,
# ~0.13) + inter-bank-swap gap (19.6 ms, 1.96 normal CIA #1 dispatches
# via the audio fallthrough). Total ≈ 3.22 of 3.30 underflows captured
# (~97 % vs. 67 % baseline). Residual ~3 % loss is below the host
# audio sample queue's hysteresis and not audibly distinguishable from
# the C baseline (REU audio alone with no bank-swap).
#
# Wall-time cost: each chunk adds ~17 cycles of inner-loop overhead
# (length reload + 5-cyc DEC zp + 3-cyc BNE) on top of the 100-cycle
# halt, plus ~50 µs NMI service per chunk on average. Total bank-swap
# wall ≈ 18 ms (vs. ~10 ms for the monolithic merged variant). On NTSC
# (16.6 ms frame) this means bank-swap straddles the frame boundary —
# but the host already produces mhires frames at ~30 fps (per-cell
# quantization is the bottleneck), so the effective display rate is
# unchanged.
#
# Zero-page: the chunk counter lives at $FB (the canonical 4-byte
# user-free block $FB-$FE). c64cast uses no other zero-page slots.
BANK_SWAP_CHUNK_SIZE = 100  # bytes per chunked REC DMA
_BITMAP_CHUNKS = 8000 // BANK_SWAP_CHUNK_SIZE  # 80
_SCREEN_CHUNKS = 1000 // BANK_SWAP_CHUNK_SIZE  # 10
_COLOR_CHUNKS = 1000 // BANK_SWAP_CHUNK_SIZE  # 10
_CHUNK_COUNTER_ZP = 0xFB  # zero-page chunk counter

# The dispatcher is too large for the original 1-byte BEQ displacement
# trick (the audio fallthrough sits ~170 bytes deep). The first two
# branches are inverted to BNE-skip-then-JMP form so they can reach
# any offset in the handler. The rest of the branches stay within
# single-byte range (chunk loops + copy loops are all ≤ 19 bytes;
# pump-check BEQ is +3).
#
# Byte layout (offsets relative to $C500 install address):
#   0-20    Header: raster vs audio dispatch + ready-flag gate
#   21-64   Bitmap family: copy loop (11) + counter init (4) + chunk loop
#           (19) + end-of-family pump check (10) = 44 B
#   65-108  Screen family: same shape
#   109-152 Color family: same shape
#   153-169 Tail: bg0, $DD00 bank swap, clear ready flag
#   170-172 chain: JMP $EA31
#   173-175 audio_fallthrough: JMP $C100
#
# Branch displacements (all verified by the assertion below):
#   offset 5   BNE +3 → 10     (skip JMP audio)
#   offset 7   JMP $C5AD       (audio_fallthrough = $C500 + 173)
#   offset 16  BNE +3 → 21     (skip JMP chain)
#   offset 18  JMP $C5AA       (chain = $C500 + 170)
#   offset 30  BPL -9 → 23     (bitmap copy loop body)
#   offset 53  BNE -19 → 36    (bitmap chunk loop body)
#   offset 60  BEQ +3 → 65     (bitmap end-of-family pump check)
#   offset 74  BPL -9 → 67     (screen copy loop body)
#   offset 97  BNE -19 → 80    (screen chunk loop body)
#   offset 104 BEQ +3 → 109    (screen end-of-family pump check)
#   offset 118 BPL -9 → 111    (color copy loop body)
#   offset 141 BNE -19 → 124   (color chunk loop body)
#   offset 148 BEQ +3 → 153    (color end-of-family pump check)
_PUMP_BODY_LO = 0x80  # REU_PUMP_BODY_SUBROUTINE_ADDR low byte ($C180 & $FF)
_PUMP_BODY_HI = 0xC1  # REU_PUMP_BODY_SUBROUTINE_ADDR high byte ($C180 >> 8)
MHIRES_BANK_SWAP_CHUNKED_PLUS_AUDIO_IRQ_HANDLER = bytes(
    [
        # --- Header: dispatch raster vs audio ---
        0xAD,
        0x19,
        0xD0,  # 0   LDA $D019
        0x29,
        0x01,  # 3   AND #$01
        0xD0,
        0x03,  # 5   BNE +3 → 10
        0x4C,
        0xAD,
        0xC5,  # 7   JMP $C5AD (audio fallthrough)
        0x8D,
        0x19,
        0xD0,  # 10  STA $D019 (ack raster)
        0xAD,
        0x17,
        0xC7,  # 13  LDA $C717 (ready flag)
        0xD0,
        0x03,  # 16  BNE +3 → 21
        0x4C,
        0xAA,
        0xC5,  # 18  JMP $C5AA (chain to kernal)
        # --- BITMAP family: 80 chunks × 100 bytes = 8000 bytes ---
        # Copy 5 bytes ($DF02..$DF06 = main lo/hi + REU lo/mi/hi). Length
        # ($DF07/$DF08) is set per-chunk, NOT here.
        0xA2,
        0x04,  # 21  LDX #$04
        0xBD,
        0x00,
        0xC7,  # 23  LDA $C700,X
        0x9D,
        0x02,
        0xDF,  # 26  STA $DF02,X
        0xCA,  # 29  DEX
        0x10,
        0xF7,  # 30  BPL -9 → 23
        0xA9,
        _BITMAP_CHUNKS,  # 32  LDA #80
        0x85,
        _CHUNK_COUNTER_ZP,  # 34  STA $FB
        0xA9,
        BANK_SWAP_CHUNK_SIZE,  # 36  LDA #100 (chunk lo)
        0x8D,
        0x07,
        0xDF,  # 38  STA $DF07
        0xA9,
        0x00,  # 41  LDA #$00 (chunk hi)
        0x8D,
        0x08,
        0xDF,  # 43  STA $DF08
        0xA9,
        0x91,  # 46  LDA #$91 (REU exec REU→C64)
        0x8D,
        0x01,
        0xDF,  # 48  STA $DF01 (trigger ~100 cyc halt)
        0xC6,
        _CHUNK_COUNTER_ZP,  # 51  DEC $FB
        0xD0,
        0xED,  # 53  BNE -19 → 36
        # End-of-bitmap pump check: ack CIA #1 if pending, run pump body.
        # JSR clobbers $DF02..$DF06 — safe because the next family's copy
        # loop re-loads them from the frame tracker.
        0xAD,
        0x0D,
        0xDC,  # 55  LDA $DC0D (ack CIA #1 ICR)
        0x29,
        0x01,  # 58  AND #$01 (timer A bit)
        0xF0,
        0x03,  # 60  BEQ +3 → 65 (skip JSR)
        0x20,
        _PUMP_BODY_LO,
        _PUMP_BODY_HI,  # 62  JSR $C180 (pump body)
        # --- SCREEN family: 10 chunks × 100 bytes = 1000 bytes ---
        0xA2,
        0x04,  # 65  LDX #$04
        0xBD,
        0x07,
        0xC7,  # 67  LDA $C707,X
        0x9D,
        0x02,
        0xDF,  # 70  STA $DF02,X
        0xCA,  # 73  DEX
        0x10,
        0xF7,  # 74  BPL -9 → 67
        0xA9,
        _SCREEN_CHUNKS,  # 76  LDA #10
        0x85,
        _CHUNK_COUNTER_ZP,  # 78  STA $FB
        0xA9,
        BANK_SWAP_CHUNK_SIZE,  # 80  LDA #100
        0x8D,
        0x07,
        0xDF,  # 82  STA $DF07
        0xA9,
        0x00,  # 85  LDA #$00
        0x8D,
        0x08,
        0xDF,  # 87  STA $DF08
        0xA9,
        0x91,  # 90  LDA #$91
        0x8D,
        0x01,
        0xDF,  # 92  STA $DF01 (trigger)
        0xC6,
        _CHUNK_COUNTER_ZP,  # 95  DEC $FB
        0xD0,
        0xED,  # 97  BNE -19 → 80
        # End-of-screen pump check.
        0xAD,
        0x0D,
        0xDC,  # 99  LDA $DC0D
        0x29,
        0x01,  # 102 AND #$01
        0xF0,
        0x03,  # 104 BEQ +3 → 109
        0x20,
        _PUMP_BODY_LO,
        _PUMP_BODY_HI,  # 106 JSR $C180
        # --- COLOR family: 10 chunks × 100 bytes = 1000 bytes ---
        0xA2,
        0x04,  # 109 LDX #$04
        0xBD,
        0x0E,
        0xC7,  # 111 LDA $C70E,X
        0x9D,
        0x02,
        0xDF,  # 114 STA $DF02,X
        0xCA,  # 117 DEX
        0x10,
        0xF7,  # 118 BPL -9 → 111
        0xA9,
        _COLOR_CHUNKS,  # 120 LDA #10
        0x85,
        _CHUNK_COUNTER_ZP,  # 122 STA $FB
        0xA9,
        BANK_SWAP_CHUNK_SIZE,  # 124 LDA #100
        0x8D,
        0x07,
        0xDF,  # 126 STA $DF07
        0xA9,
        0x00,  # 129 LDA #$00
        0x8D,
        0x08,
        0xDF,  # 131 STA $DF08
        0xA9,
        0x91,  # 134 LDA #$91
        0x8D,
        0x01,
        0xDF,  # 136 STA $DF01 (trigger)
        0xC6,
        _CHUNK_COUNTER_ZP,  # 139 DEC $FB
        0xD0,
        0xED,  # 141 BNE -19 → 124
        # End-of-color pump check.
        0xAD,
        0x0D,
        0xDC,  # 143 LDA $DC0D
        0x29,
        0x01,  # 146 AND #$01
        0xF0,
        0x03,  # 148 BEQ +3 → 153
        0x20,
        _PUMP_BODY_LO,
        _PUMP_BODY_HI,  # 150 JSR $C180
        # --- TAIL: bg0, bank swap, clear ready ---
        0xAD,
        0x15,
        0xC7,  # 153 LDA $C715 (bg0)
        0x8D,
        0x21,
        0xD0,  # 156 STA $D021
        0xAD,
        0x16,
        0xC7,  # 159 LDA $C716 (bank value)
        0x8D,
        0x00,
        0xDD,  # 162 STA $DD00 (swap VIC bank)
        0xA9,
        0x00,  # 165 LDA #$00
        0x8D,
        0x17,
        0xC7,  # 167 STA $C717 (clear ready flag)
        # --- EXIT PATHS ---
        0x4C,
        0x31,
        0xEA,  # 170 JMP $EA31 (chain to kernal)
        0x4C,
        AUDIO_HANDLER_INSTALL_ADDR & 0xFF,
        (AUDIO_HANDLER_INSTALL_ADDR >> 8) & 0xFF,  # 173 JMP $C100
    ]
)
assert len(MHIRES_BANK_SWAP_CHUNKED_PLUS_AUDIO_IRQ_HANDLER) == 176, (
    "MHIRES_BANK_SWAP_CHUNKED_PLUS_AUDIO_IRQ_HANDLER length changed — the "
    "JMP targets at offsets 7 ($C500+173) and 18 ($C500+170), the BPL "
    "offsets in the 3 copy loops, the BNE offsets in the 3 chunk loops, "
    "and the BEQ +3 offsets in the 3 end-of-family pump checks must all "
    "be recomputed before changing. See the offset comments in the byte "
    "column."
)
# Sanity-check the cross-module address coupling between the chunked
# dispatcher (constructed here from raw bytes) and the pump body
# subroutine address (imported from audio_handlers.py at the top of the
# module). If audio_handlers.py ever relocates REU_PUMP_BODY_SUBROUTINE_ADDR away from
# $C180, the JSR operands inside the dispatcher above must move with it.
assert REU_PUMP_BODY_SUBROUTINE_ADDR == 0xC180
assert _PUMP_BODY_LO == (REU_PUMP_BODY_SUBROUTINE_ADDR & 0xFF)
assert _PUMP_BODY_HI == ((REU_PUMP_BODY_SUBROUTINE_ADDR >> 8) & 0xFF)


# --- Host-DMA double-buffer swap IRQ handler (no-REU backends, e.g. TeensyROM) -
# The minimal sibling of the REU bank-swap handlers above. On a backend whose bus
# DMA is too slow to rewrite a full bitmap frame in the VISIBLE bank without
# tearing (TeensyROM serial/TCP both ~106 KiB/s — the bus, not the link, is the
# wall), the host writes each frame's bitmap+screen straight into the OFF-screen
# VIC bank over the normal host-DMA write_region path, then arms this IRQ to flip
# $DD00 at vblank. The visible bank is never touched mid-display, so every shown
# frame is whole — tear-free at the same frame rate.
#
# Unlike the REU handlers, this does NO in-IRQ DMA — it just writes $D021 (bg0)
# and flips $DD00 from a tiny 3-byte tracker. So the swap lands cleanly inside
# vblank with no past-vblank overrun → no shimmer, and text overlays folded into
# the bitmap render crisply (which the REU path can't claim). NMI audio lives on
# the $FFFA vector, independent of this $0314 raster IRQ, so they coexist; the
# handler chains to kernal $EA31 so SCNKEY keeps $028D live for the key pollers.
#
# Compact tracker at $C700 (reuses FRAME_TRACKER_ADDR — never live alongside the
# REU tracker, since a scene has exactly one display mode):
#   $C700 : bg0 value to write to $D021
#   $C701 : pending bank value ($97 = bank 0, $95 = bank 2)
#   $C702 : ready flag (1 = frame staged) — host arms, handler clears
#
# A/X/Y survive: kernal $FF48 saved them before vectoring through $0314, and we
# only touch A (restored by $EA81's PLA). Offsets must be exact: every branch
# targets the JMP $EA31 chain at offset 42. The assert below catches length
# drift.
HOSTDMA_TRACKER_OFF_BG0 = 0  # $C700
HOSTDMA_TRACKER_OFF_BANK = 1  # $C701
HOSTDMA_TRACKER_OFF_READY = 2  # $C702
HOSTDMA_TRACKER_LEN = 3


# --- The raster window gate, shared by both host-DMA swap handlers ----------
# A host DMA write halts the 6510 for ~1.02 us/byte, so an 8000-byte bitmap
# push stalls it ~8.2 ms ≈ 128 raster lines. A raster IRQ that falls inside a
# halt does not run until the halt ends, and its $DD00 lands deep in the
# visible picture — the top band still shows the previous frame while the rest
# shows the new one. Measured on an Ultimate 64 over HDMI: 5.3% of flicker
# frames torn, seam at a median 30% of picture height.
#
# The host cannot avoid this by scheduling its writes, because it cannot learn
# where the raster is: polling $D012 over REST wedges the machine during
# playback, and extrapolating from a clock drifts past a whole field within
# seconds. So the decision is made on the C64, by the handler, from the one
# reading that is always current — $D012 at the moment it actually runs.
#
# Out of window the handler acks the IRQ and returns WITHOUT clearing the
# ready flag, so the staged frame simply commits on a later field. A deferred
# frame holds the previous one a field longer; it never shows two at once.
#
# Committing is invisible from the IRQ line through RASTER_COMMIT_LAST_SAFE_LINE,
# i.e. $D012 in [248, 255] u [0, 45]. Adding 8 rotates that split range into a
# contiguous 0..53, which is why the check costs one compare and one branch
# instead of two of each.
_RASTER_GATE_BIAS = (0x100 - RASTER_VBLANK_LINE) & 0xFF  # $08
_RASTER_GATE_LIMIT = _RASTER_GATE_BIAS + RASTER_COMMIT_LAST_SAFE_LINE + 1  # $36
assert _RASTER_GATE_LIMIT <= 0xFF

# $D012 is 8 bits and cannot tell line n from line n+256, but every line that
# aliases lands in the safe set on both systems: NTSC 256-261 and PAL 256-301
# read back as 0-45, and all of them really are in vblank. PAL 302-311 alias
# onto 46-55 and are conservatively rejected, which only forgoes a commit
# opportunity. No genuinely unsafe line (46-247) can alias into the window,
# since none of them exceed 255. One formulation is correct for PAL and NTSC.

HOSTDMA_SWAP_IRQ_HANDLER = bytes(
    [
        0xAD,
        0x19,
        0xD0,  # 0  LDA $D019         ; VIC IRQ status
        0x29,
        0x01,  # 3  AND #$01          ; raster bit
        0xF0,
        0x23,  # 5  BEQ +35 → 42      ; not raster → chain
        0x8D,
        0x19,
        0xD0,  # 7  STA $D019         ; ack raster (A = $01)
        0xAD,
        0x02,
        0xC7,  # 10 LDA $C702         ; ready flag
        0xF0,
        0x1B,  # 13 BEQ +27 → 42      ; no new frame → chain
        0xAD,
        0x12,
        0xD0,  # 15 LDA $D012         ; where is the raster NOW?
        0x18,  # 18 CLC
        0x69,
        _RASTER_GATE_BIAS,  # 19 ADC #$08         ; 248..255 → 0..7, 0..45 → 8..53
        0xC9,
        _RASTER_GATE_LIMIT,  # 21 CMP #$36
        0xB0,
        0x11,  # 23 BCS +17 → 42      ; past the window → leave staged, chain
        0xAD,
        0x00,
        0xC7,  # 25 LDA $C700         ; bg0
        0x8D,
        0x21,
        0xD0,  # 28 STA $D021         ; set bg0
        0xAD,
        0x01,
        0xC7,  # 31 LDA $C701         ; pending bank value
        0x8D,
        0x00,
        0xDD,  # 34 STA $DD00         ; swap bank (tear-free at vblank)
        0xA9,
        0x00,  # 37 LDA #$00
        0x8D,
        0x02,
        0xC7,  # 39 STA $C702         ; clear ready flag
        0x4C,
        0x31,
        0xEA,  # 42 JMP $EA31         ; chain to kernal
    ]
)
assert len(HOSTDMA_SWAP_IRQ_HANDLER) == 45, (
    "HOSTDMA_SWAP_IRQ_HANDLER length changed — the three branch offsets (+35, "
    "+27 and +17, all targeting the JMP $EA31 chain at offset 42) must be "
    "recomputed before changing. See the offsets in the byte-comment column."
)


# ---------------------------------------------------------------------------
# Flicker blend ([color].flicker_blend) — page-flip every field
# ---------------------------------------------------------------------------
# The host-DMA sibling above, plus an unconditional per-field toggle of the
# $D018 screen-matrix nibble between the two page offsets (c64.D018_HIRES_PAGE_A
# / _B). Two screen pages holding different colour nibbles over one shared
# bitmap therefore alternate at the VIC field rate, and the eye fuses each cell's
# pair into a colour the VIC cannot draw. See video/flicker.py for which pairs
# are eligible and why.
#
# The toggle is deliberately ahead of the ready-flag check, and ahead of the
# raster gate: the alternation is the C64's job and must free-run at the field
# rate whatever the host is doing, which is the whole reason this does not need
# 50-60 fps over the link. Gating it would drop fields out of the fusion cadence
# — a worse artifact than a late page flip, which only mistimes the blended
# cells' colours rather than showing two frames of bitmap at once. Only the
# double-buffer commit ($DD00 + $D021) waits on a staged frame and a safe raster.
#
# That commit is additionally gated on landing in phase 0, so a bank swap can
# never transpose the A/B page roles — without it a swap arriving on an odd
# field would put field A's nibbles on field B's slot for the rest of the scene,
# which is invisible on a still frame and reads as a colour shift on motion.
#
# X is used as the page index and is NOT saved here: kernal $FF48 pushed A/X/Y
# before vectoring through $0314 and $EA81 pulls them back, the same reason the
# handler above gets away with clobbering A.
#
# Tracker at $C700 (FRAME_TRACKER_ADDR), 6 bytes:
#   $C700 : bg0 value to write to $D021
#   $C701 : pending bank value ($97 = bank 0, $95 = bank 2)
#   $C702 : ready flag (1 = frame staged) — host arms, handler clears
#   $C703 : field phase, handler-owned (toggles 0/1 every raster IRQ)
#   $C704 : $D018 for phase 0 (page A)
#   $C705 : $D018 for phase 1 (page B)
FLICKER_TRACKER_OFF_BG0 = 0  # $C700
FLICKER_TRACKER_OFF_BANK = 1  # $C701
FLICKER_TRACKER_OFF_READY = 2  # $C702
FLICKER_TRACKER_OFF_PHASE = 3  # $C703
FLICKER_TRACKER_OFF_D018 = 4  # $C704 / $C705, indexed by phase
FLICKER_TRACKER_LEN = 6

FLICKER_SWAP_IRQ_HANDLER = bytes(
    [
        0xAD,
        0x19,
        0xD0,  # 0  LDA $D019         ; VIC IRQ status
        0x29,
        0x01,  # 3  AND #$01          ; raster bit
        0xF0,
        0x35,  # 5  BEQ +53 → 60      ; not raster → chain
        0x8D,
        0x19,
        0xD0,  # 7  STA $D019         ; ack raster (A = $01)
        0xAD,
        0x03,
        0xC7,  # 10 LDA $C703         ; field phase
        0x49,
        0x01,  # 13 EOR #$01          ; flip it
        0x8D,
        0x03,
        0xC7,  # 15 STA $C703
        0xAA,  # 18 TAX               ; X = new phase (0 or 1)
        0xBD,
        0x04,
        0xC7,  # 19 LDA $C704,X       ; that phase's $D018
        0x8D,
        0x18,
        0xD0,  # 22 STA $D018         ; commit in vblank — page flip, no tear
        0xAD,
        0x02,
        0xC7,  # 25 LDA $C702         ; ready flag
        0xF0,
        0x1E,  # 28 BEQ +30 → 60      ; no new frame → chain
        0x8A,  # 30 TXA               ; phase back into A (sets Z)
        0xD0,
        0x1B,  # 31 BNE +27 → 60      ; commit only on phase 0 → chain
        0xAD,
        0x12,
        0xD0,  # 33 LDA $D012         ; where is the raster NOW?
        0x18,  # 36 CLC
        0x69,
        _RASTER_GATE_BIAS,  # 37 ADC #$08         ; 248..255 → 0..7, 0..45 → 8..53
        0xC9,
        _RASTER_GATE_LIMIT,  # 39 CMP #$36
        0xB0,
        0x11,  # 41 BCS +17 → 60      ; past the window → leave staged, chain
        0xAD,
        0x00,
        0xC7,  # 43 LDA $C700         ; bg0
        0x8D,
        0x21,
        0xD0,  # 46 STA $D021
        0xAD,
        0x01,
        0xC7,  # 49 LDA $C701         ; pending bank value
        0x8D,
        0x00,
        0xDD,  # 52 STA $DD00         ; swap bank (tear-free at vblank)
        0xA9,
        0x00,  # 55 LDA #$00
        0x8D,
        0x02,
        0xC7,  # 57 STA $C702         ; clear ready flag
        0x4C,
        0x31,
        0xEA,  # 60 JMP $EA31         ; chain to kernal
    ]
)
assert len(FLICKER_SWAP_IRQ_HANDLER) == 63, (
    "FLICKER_SWAP_IRQ_HANDLER length changed — the four branch offsets (+53, "
    "+30, +27, +17, all targeting the JMP $EA31 chain at offset 60) must be "
    "recomputed before changing. See the offsets in the byte-comment column."
)


# CIA #2 PORT_A bank-select values (also defined in c64.CIA2 but pulled
# here so the per-frame push has them as Python ints, not strings — fewer
# allocations on the hot path).
DD00_BANK_0 = CIA2.PORT_A_BANK_0  # $97
DD00_BANK_2 = CIA2.PORT_A_BANK_2  # $95

# CIA #1 ICR control words for raster-IRQ bring-up / teardown.
# CIA1_ICR_DISABLE_TIMER_A clears bit 0 of the ICR; CIA1_ICR_ENABLE_TIMER_A
# re-arms it (high bit = 1 = set bits, plus bit 0 = timer A IRQ source).
# Mirrors the audio.py CIA #2 disable/enable pattern but on CIA #1.
_CIA1_ICR_DISABLE_TIMER_A = 0x7F
_CIA1_ICR_ENABLE_TIMER_A = 0x81


def install_bank_swap_irq(
    api: C64Backend,
    handler_bytes: bytes = BANK_SWAP_IRQ_HANDLER,
    tracker_len: int = FRAME_TRACKER_LEN,
    *,
    audio_pump_active: bool = False,
    tracker_init: bytes | None = None,
) -> None:
    """Bring up the bank-swap raster IRQ.

    `handler_bytes` and `tracker_len` default to the hires-flavor 61-byte
    handler + 16-byte tracker. MultiHires passes its own (83-byte handler,
    24-byte tracker). Both flavors live at the same addresses
    (BANK_SWAP_IRQ_HANDLER_ADDR, FRAME_TRACKER_ADDR) because the two
    display modes are mutually exclusive.

    `audio_pump_active`: True when the scene also opted into REU audio
    (`use_reu_pump = true`). In that case `handler_bytes` is expected to
    be a merged dispatcher (BANK_SWAP_PLUS_AUDIO_IRQ_HANDLER or the
    mhires equivalent) whose non-raster branch JMPs to $C100 where the
    audio pump handler lives. We pre-upload a 3-byte JMP $EA31 stub at
    $C100 BEFORE hooking $0314 so the gap between this install completing
    (CIA #1 IRQ re-enabled at the end) and audio.start_for_reu_staged
    populating the real handler bytes is covered by a safe fall-through
    instead of a JMP into uninitialized RAM.

    Order matters: with both raster and CIA #1 sources masked, hook $0314,
    program the raster compare line, ack any pending raster IRQ, then
    enable raster + re-enable CIA #1. If we left CIA #1 enabled while
    swinging $0314, a stray jiffy IRQ could vector through our
    half-installed handler. Same sequence as
    [overlays/big_text.py:_install_raster_irq]."""
    if audio_pump_active:
        # Critical ordering: stub MUST be in place by the time CIA #1 is
        # re-enabled at step 6 below. Easiest correct ordering is to
        # upload it before any other write — that way ANY IRQ source firing
        # during the install sees a safe $C100, even if some future edit
        # changes the install order.
        api.write_memory_file(f"{AUDIO_HANDLER_INSTALL_ADDR:04X}", AUDIO_HANDLER_STUB)
    api.write_memory_file(f"{BANK_SWAP_IRQ_HANDLER_ADDR:04X}", handler_bytes)
    # Zero the frame tracker — ready flag (last byte) = 0 means the first
    # IRQ after install skips the DMA path until the host stages a real
    # frame.
    #
    # `tracker_init` overrides those zeros for handlers with a field the IRQ
    # *reads* unconditionally rather than only behind the ready flag — the
    # flicker handler's $D018 page pair. Zeros there would point VIC at the
    # $0000 matrix offset for the field or two before the first frame stages,
    # so the seed has to be in place before step 5 arms the raster source.
    tracker = bytes(tracker_len) if tracker_init is None else tracker_init
    if len(tracker) != tracker_len:
        raise ValueError(f"tracker_init must be {tracker_len} bytes, got {len(tracker)}")
    api.write_memory_file(f"{FRAME_TRACKER_ADDR:04X}", tracker)
    # 1) Mask CIA #1 (jiffy IRQ would otherwise vector through $0314 mid-install).
    api.write_memory(f"{CIA1.ICR:04X}", f"{_CIA1_ICR_DISABLE_TIMER_A:02X}")
    # 2) Disable VIC IRQ sources (raster + sprite collisions + light pen).
    api.write_memory("D01A", "00")
    # 3) Hook $0314/$0315 → our handler. write_regs packs both bytes into
    #    one DMA so the vector is never half-updated on the wire.
    api.write_regs(
        f"{VECTORS.IRQ:04X}",
        BANK_SWAP_IRQ_HANDLER_ADDR & 0xFF,
        (BANK_SWAP_IRQ_HANDLER_ADDR >> 8) & 0xFF,
    )
    # 4) Program the raster compare register. RASTER_VBLANK_LINE = 248
    #    sits at the top of VBLANK on both PAL and NTSC — VIC isn't
    #    rendering visible pixels, so the bank swap + per-frame REU DMAs
    #    happen entirely outside the rendered area. $D011 bit 7 is the
    #    raster MSB; we leave it 0 (lines 0-255 only).
    api.write_memory("D012", f"{RASTER_VBLANK_LINE:02X}")
    # 5) Ack any latent raster flag, then enable raster IRQ source.
    api.write_memory("D019", "01")
    api.write_memory("D01A", "01")
    # 6) Re-enable CIA #1 jiffy IRQ — kernal keyboard scan etc.
    api.write_memory(f"{CIA1.ICR:04X}", f"{_CIA1_ICR_ENABLE_TIMER_A:02X}")


def uninstall_bank_swap_irq(api: C64Backend) -> None:
    """Tear down the bank-swap raster IRQ. Mirror of install_bank_swap_irq
    in reverse, plus restore $DD00 = bank 0 so the next scene's setup
    sees the kernal-default VIC bank. Best-effort: any failure logs and
    swallows so teardown doesn't abort a multi-scene transition."""
    try:
        # 1) Mask CIA #1 + disable VIC IRQ first so no IRQ source can fire
        #    into the about-to-be-unhooked handler.
        api.write_memory(f"{CIA1.ICR:04X}", f"{_CIA1_ICR_DISABLE_TIMER_A:02X}")
        api.write_memory("D01A", "00")
        # 2) Restore $0314/$0315 → kernal $EA31.
        api.write_regs(
            f"{VECTORS.IRQ:04X}", KERNAL.IRQ_HANDLER & 0xFF, (KERNAL.IRQ_HANDLER >> 8) & 0xFF
        )
        # 3) Ack any pending raster IRQ flag so the next $D019 read is clean.
        api.write_memory("D019", "01")
        # 4) Restore VIC bank to 0 (kernal default) so the next scene
        #    paints into the addresses it expects.
        api.write_memory(f"{CIA2.PORT_A:04X}", f"{DD00_BANK_0:02X}")
        # 5) Re-enable CIA #1 jiffy IRQ — keyboard scan must keep running
        #    for the C= / CTRL / SHIFT poller.
        api.write_memory(f"{CIA1.ICR:04X}", f"{_CIA1_ICR_ENABLE_TIMER_A:02X}")
    except Exception as e:
        log.debug("bank-swap IRQ teardown: %s", e)


def push_bitmap_via_reu(
    api: C64Backend, bitmap_bytes: bytes, screen_bytes: bytes, target_bank: int
) -> None:
    """REUWRITE bitmap + screen into REU staging, then DMAWRITE a 16-byte
    frame tracker to $C700-$C70F. The C64-side raster IRQ at vblank
    reads the tracker, triggers the two REU→main DMAs into the
    off-screen bank, and flips $DD00 — all without any further host
    involvement.

    target_bank: 0 = bank 0 (dest $2000 + $0400, $DD00 = $97),
                 1 = bank 2 (dest $A000 + $8400, $DD00 = $95).

    Per-frame host work: 2 REUWRITEs (bus-clean) + 1 DMAWRITE (16 bytes,
    halts C64 bus for ~16 cycles — negligible vs the ~9000 cycles the
    REU→main DMAs themselves consume). The big halts happen at vblank
    on a deterministic 60-Hz schedule (kernal IRQ tick) rather than at
    Python-jittered wall-clock instants — see u64_reu_socket_dma.md
    Phase 2 v2 for the perceptual argument."""
    if target_bank == 0:
        bitmap_dest = VIC_BANK_0.BITMAP
        screen_dest = VIC_BANK_0.SCREEN
        pending_value = DD00_BANK_0
    else:
        bitmap_dest = VIC_BANK_2.BITMAP
        screen_dest = VIC_BANK_2.SCREEN
        pending_value = DD00_BANK_2
    # 1. Stage bitmap + screen into REU SRAM (bus-clean — no C64 halt).
    api.reu_write(REU_VIDEO_BITMAP_BASE, bitmap_bytes)
    api.reu_write(REU_VIDEO_BITMAP_SCREEN_BASE, screen_bytes)
    # 2. Pack the 16-byte frame tracker. Order matches the IRQ handler's
    #    layout exactly; ready flag = 1 is the LAST byte, so even if the
    #    IRQ fired mid-write (it can't — the DMAWRITE arrives atomically
    #    on the C64 side after the FIFO drain) the regs would always be
    #    consistent before ready flips.
    tracker = bytes(
        [
            bitmap_dest & 0xFF,
            (bitmap_dest >> 8) & 0xFF,
            REU_VIDEO_BITMAP_BASE & 0xFF,
            (REU_VIDEO_BITMAP_BASE >> 8) & 0xFF,
            (REU_VIDEO_BITMAP_BASE >> 16) & 0xFF,
            REU_VIDEO_BITMAP_LEN & 0xFF,
            (REU_VIDEO_BITMAP_LEN >> 8) & 0xFF,
            screen_dest & 0xFF,
            (screen_dest >> 8) & 0xFF,
            REU_VIDEO_BITMAP_SCREEN_BASE & 0xFF,
            (REU_VIDEO_BITMAP_SCREEN_BASE >> 8) & 0xFF,
            (REU_VIDEO_BITMAP_SCREEN_BASE >> 16) & 0xFF,
            REU_VIDEO_BITMAP_SCREEN_LEN & 0xFF,
            (REU_VIDEO_BITMAP_SCREEN_LEN >> 8) & 0xFF,
            pending_value,
            0x01,  # ready flag
        ]
    )
    api.write_memory_file(f"{FRAME_TRACKER_ADDR:04X}", tracker)


def push_mhires_via_reu(
    api: C64Backend,
    bitmap_bytes: bytes,
    screen_bytes: bytes,
    color_bytes: bytes,
    bg0: int,
    target_bank: int,
) -> None:
    """MultiHires bank-swap push. Extends push_bitmap_via_reu with a third
    REUWRITE for the 1000-byte color RAM, plus a bg0 byte in the tracker
    that the IRQ writes to $D021.

    target_bank: 0 = bank 0 (dest $2000 + $0400, $DD00 = $97),
                 1 = bank 2 (dest $A000 + $8400, $DD00 = $95).

    Per-frame host work: 3 REUWRITEs (bus-clean) + 1 DMAWRITE (24 bytes,
    halts C64 bus ~24 cycles — negligible). The big halts (bitmap ~8000,
    screen ~1000, color ~1000 = ~10000 cycles total) happen on the C64
    side, triggered by the kernal IRQ at vblank. The color DMA's
    write-to-shared-$D800 means a brief c3-mismatch window across the
    bank-swap line — see MHIRES_BANK_SWAP_IRQ_HANDLER for the timing
    analysis."""
    if target_bank == 0:
        bitmap_dest = VIC_BANK_0.BITMAP
        screen_dest = VIC_BANK_0.SCREEN
        pending_value = DD00_BANK_0
    else:
        bitmap_dest = VIC_BANK_2.BITMAP
        screen_dest = VIC_BANK_2.SCREEN
        pending_value = DD00_BANK_2
    color_dest = SCREEN.COLOR_RAM  # $D800 — not banked, single shared SRAM
    # 1. Stage bitmap + screen + color into REU SRAM (all bus-clean — no
    #    C64 halts; ARM-side memcpy into FPGA SRAM).
    api.reu_write(REU_VIDEO_BITMAP_BASE, bitmap_bytes)
    api.reu_write(REU_VIDEO_BITMAP_SCREEN_BASE, screen_bytes)
    api.reu_write(REU_VIDEO_BITMAP_COLOR_BASE, color_bytes)
    # 2. Pack the 24-byte frame tracker. Order matches the IRQ handler's
    #    layout exactly; ready flag = 1 is the LAST byte, so the IRQ
    #    handler can rely on the regs being consistent whenever it sees
    #    ready=1.
    tracker = bytes(
        [
            # bitmap regs: $DF02..$DF08 packed [c64_lo, c64_hi, reu_lo, reu_mi,
            # reu_hi, len_lo, len_hi]
            bitmap_dest & 0xFF,
            (bitmap_dest >> 8) & 0xFF,
            REU_VIDEO_BITMAP_BASE & 0xFF,
            (REU_VIDEO_BITMAP_BASE >> 8) & 0xFF,
            (REU_VIDEO_BITMAP_BASE >> 16) & 0xFF,
            REU_VIDEO_BITMAP_LEN & 0xFF,
            (REU_VIDEO_BITMAP_LEN >> 8) & 0xFF,
            # screen regs
            screen_dest & 0xFF,
            (screen_dest >> 8) & 0xFF,
            REU_VIDEO_BITMAP_SCREEN_BASE & 0xFF,
            (REU_VIDEO_BITMAP_SCREEN_BASE >> 8) & 0xFF,
            (REU_VIDEO_BITMAP_SCREEN_BASE >> 16) & 0xFF,
            REU_VIDEO_BITMAP_SCREEN_LEN & 0xFF,
            (REU_VIDEO_BITMAP_SCREEN_LEN >> 8) & 0xFF,
            # color regs
            color_dest & 0xFF,
            (color_dest >> 8) & 0xFF,
            REU_VIDEO_BITMAP_COLOR_BASE & 0xFF,
            (REU_VIDEO_BITMAP_COLOR_BASE >> 8) & 0xFF,
            (REU_VIDEO_BITMAP_COLOR_BASE >> 16) & 0xFF,
            REU_VIDEO_BITMAP_COLOR_LEN & 0xFF,
            (REU_VIDEO_BITMAP_COLOR_LEN >> 8) & 0xFF,
            # bg0, bank value, ready flag
            bg0 & 0xFF,
            pending_value,
            0x01,
        ]
    )
    api.write_memory_file(f"{FRAME_TRACKER_ADDR:04X}", tracker)


def push_screen_via_reu(api: C64Backend, screen_bytes: bytes, dest_addr: int) -> None:
    """REUWRITE the screen bytes to REU, then trigger a REU→main DMA into
    `dest_addr` (the screen RAM location for the current VIC bank — $0400
    for bank 0, $8400 for bank 2). Used by the REU-staged char-mode push.
    Each frame is a one-shot transfer (no auto-increment across triggers),
    so the REU source offset stays pinned at REU_VIDEO_SCREEN_BASE — the
    REUWRITE in step 1 overwrites the staging area each frame."""
    # 1. Stage the new screen into REU SRAM (clean — no C64 bus halt).
    api.reu_write(REU_VIDEO_SCREEN_BASE, screen_bytes)
    # 2. Configure REU source (REU_VIDEO_SCREEN_BASE, 24-bit), dest
    # (dest_addr, 16-bit), length (1000 bytes), addr-control (auto-inc
    # both — default 0). write_regs packs contiguous register writes into
    # one DMA command, so REU regs go in 3 commands instead of 7.
    api.write_regs(f"{REU.C64_ADDR_LO:04X}", dest_addr & 0xFF, (dest_addr >> 8) & 0xFF)
    api.write_regs(
        f"{REU.REU_ADDR_LO:04X}",
        REU_VIDEO_SCREEN_BASE & 0xFF,
        (REU_VIDEO_SCREEN_BASE >> 8) & 0xFF,
        (REU_VIDEO_SCREEN_BASE >> 16) & 0xFF,
    )
    api.write_regs(
        f"{REU.LENGTH_LO:04X}", REU_VIDEO_SCREEN_LEN & 0xFF, (REU_VIDEO_SCREEN_LEN >> 8) & 0xFF
    )
    # 3. Trigger. The CPU halts for ~1000 cycles (1 byte/cycle) while the
    # REU→main DMA copies the staged frame into screen RAM. This is the
    # only bus-halt event in the REU-staged char push (REUWRITE in step 1
    # is bus-clean; color RAM uses the regular delta cache).
    api.write_memory(f"{REU.COMMAND:04X}", f"{REU.CMD_FETCH_EXEC:02X}")
