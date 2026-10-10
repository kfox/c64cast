"""The C64-side IRQ-handler layer for tear-free double-buffered video.

The 6502 machine code the bitmap modes in `modes/` upload and drive per frame:
the $C500 bank-swap raster IRQ handlers (hires, mhires, their chunked
REU-audio merged dispatchers, and the host-DMA page-flip sibling
for no-REU backends), the $C700 frame-tracker layouts each handler reads
at vblank, the REU staging addresses near 14 MB, and the bring-up /
teardown plus per-frame push helpers that stage a frame and arm the
tracker. Pure Python over C64Backend — no numpy, no cv2 — so the whole
module runs under mypy --strict.

Nothing here decides WHEN a pipeline engages: that is scene_factory's
resolve_use_reu_staged / resolve_double_buffer, and the DisplayMode classes
own the per-frame compose + call order.

See docs/architecture/video-color.md#modes_irqpy--c64-side-irq-handlers--reu-push-helpers.
"""

from __future__ import annotations

import logging
import time

from c64cast.audio.audio_handlers import (
    REU_PUMP_BODY_SUBROUTINE_ADDR,
    REU_PUMP_HANDLER_ADDR,
    REU_PUMP_HANDLER_STUB,
)
from c64cast.hw.asm6502 import assemble
from c64cast.hw.backend import C64Backend
from c64cast.hw.c64 import (
    CIA1,
    CIA2,
    KERNAL,
    NMI_SAFE_MIN_PERIOD_CYCLES,
    RASTER_COMMIT_LAST_SAFE_LINE,
    RASTER_VBLANK_LINE,
    REU,
    SCREEN,
    VECTORS,
    VIC_BANK_0,
    VIC_BANK_2,
    halt_quantum_bytes,
)
from c64cast.hw.irq_unhook import confirm, unhook_raster_irq

log = logging.getLogger(__name__)


# The char-mode REU-staged screen push: 1000 bytes to REU SRAM via socket DMA
# opcode 0xFF07 (REUWRITE — bus-clean, no SID perturbation), then a REU→main
# DMA drops them into VIC's screen RAM in one shot. Color RAM ($D800) is never
# banked and stays on the regular DMAWRITE path.
#
# Single-buffer: the REU→main write lands in the currently-displayed $0400, so
# the screen is stomped during the transfer — one frame's artifact at most.
#
# This path drives the REU controller's REC registers from the host in four
# separate DMA writes, while the REU audio pump drives them from a CIA #1 IRQ
# on the C64, and that IRQ can run between any two of the writes. Neither pump
# variant survives it: the plain one DMAs audio into screen RAM and onward
# from wherever the push left $DF02, and the tracked one reloads every register
# mid-sequence, so the push's trigger fires on a mix of its values and the
# pump's — a dropped frame, or the screen DMAd into the audio ring. So the two
# never run together — see reu_pump_skips_irq_hook.
REU_VIDEO_SCREEN_BASE = 0xE00000  # 14 MB in — way past any REU audio region
REU_VIDEO_SCREEN_LEN = SCREEN.N_CELLS  # 1000 bytes of PETSCII screen codes

# The REU-staged bitmap pipeline (double-buffer, bank-swap). Each frame is
# REUWRITE-staged into REU SRAM (bus-clean), then a pair of REU→main DMAs drop
# the bitmap + screen into the OFF-SCREEN VIC bank's addresses while the
# on-screen bank keeps being rendered (no visible tearing during the transfer).
# A C64-side raster IRQ at line $FB does the copy and, on a later field, writes
# the new $DD00 value to flip which bank VIC fetches from — a 1-cycle swap,
# held to the raster gate. The C64 side picks the bank, not the host: see
# BANK_SWAP_STATE_ADDR.
#
# Memory map (both banks always reserved while this path is active):
#   Bank 0: bitmap $2000-$3F3F, screen $0400-$07E7
#   Bank 2: bitmap $A000-$BF3F, screen $8400-$87E7
#   Bank 1 unchanged: audio ring at $4000-$5FFF
#   Color RAM at $D800 unused by hires (color encoded in screen RAM nibbles).
#
# REU staging layout, one slot per frame (slot k at k * REU_VIDEO_SLOT_STRIDE):
#   $E10000-$E11F3F  bitmap staging (8000 bytes)
#   $E12000-$E123E7  screen staging (1000 bytes)
#   $E13000-$E133E7  color staging (1000 bytes, mhires only)
#
# The host rotates through REU_VIDEO_SLOTS slots and the tracker names the one
# it just filled. A single slot let the host overwrite a frame while the C64
# was still copying it, and the copy committed with the top rows of one frame
# over the bottom rows of the next.
#
# A slot is in use from the snapshot to the end of its commit, which reads
# color RAM from it: the copy (about 2.4 fields on mhires with audio NMIs),
# then up to two more fields for the raster window, under
# _REU_SLOT_MAX_IN_USE_S. The snapshot can be up to one host frame old, and the
# host refills a slot REU_VIDEO_SLOTS frames after it last filled it, so the
# count has to cover that window at the fastest push rate, 60 fps. Three slots
# covered it only up to about the default bitmap caps, and an explicit
# target_fps above them refilled a slot the C64 was still reading.
_REU_SLOT_MAX_IN_USE_S = 0.1
_REU_SLOT_MAX_PUSH_FPS = 60
#
# Coexistence: shares the REC controller and $0314 with the REU audio pump.
# The merged dispatchers below are what let the two run together — one $0314
# hook servicing both IRQ sources.
REU_VIDEO_BITMAP_BASE = 0xE10000
REU_VIDEO_BITMAP_LEN = SCREEN.BITMAP_BYTES  # 8000 bytes
REU_VIDEO_BITMAP_SCREEN_BASE = 0xE12000  # 1000-byte screen for hires
REU_VIDEO_BITMAP_SCREEN_LEN = SCREEN.N_CELLS
# MultiHires adds per-cell color RAM ($D800) on top of bitmap+screen. $D800 is
# not VIC-banked — one shared SRAM whatever the displayed bank — so the IRQ
# handler copies it right after the bank swap, ahead of the raster (see
# _bank_swap_dispatcher).
REU_VIDEO_BITMAP_COLOR_BASE = 0xE13000  # 1000-byte color RAM staging
REU_VIDEO_BITMAP_COLOR_LEN = SCREEN.N_CELLS
REU_VIDEO_SLOTS = 16
REU_VIDEO_SLOT_STRIDE = 0x4000
# Host frames between a slot's refill and the newest snapshot that can name
# it, at the fastest push rate; twice the in-use window leaves room for pushes
# that bunch up after a host stall.
assert (REU_VIDEO_SLOTS - 1) / _REU_SLOT_MAX_PUSH_FPS >= 2 * _REU_SLOT_MAX_IN_USE_S
assert REU_VIDEO_BITMAP_COLOR_BASE + REU_VIDEO_BITMAP_COLOR_LEN <= (
    REU_VIDEO_BITMAP_BASE + REU_VIDEO_SLOT_STRIDE
)
# The highest REU byte any slot reaches; hw_provision sizes the REU past it.
REU_VIDEO_TOP = (
    REU_VIDEO_BITMAP_COLOR_BASE
    + (REU_VIDEO_SLOTS - 1) * REU_VIDEO_SLOT_STRIDE
    + REU_VIDEO_BITMAP_COLOR_LEN
    - 1
)

# C64-side bank-swap raster IRQ handler. Lives at $C500 (audio_handlers.py owns
# $C000-$C2FF for NMI DAC + REU pump handlers; api.py uses $C300/$C400
# for the SID player + re-INIT stub; big_text.py uses $C000-$C01F). The frame tracker at
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
#   $C70E       : border value to write to $D020
#   $C70F       : ready flag (1 = frame staged, 0 = no new frame)
#
# The host sets $C70F = 1 (last byte of the DMAWRITE blob) to arm, and the
# handler clears it when it starts copying that frame. A skipped IRQ (ready=0,
# nothing copied) just chains straight to kernal — costs ~20 cycles.
FRAME_TRACKER_LEN = 16
TRACKER_OFF_BITMAP_REGS = 0  # 7 bytes
TRACKER_OFF_SCREEN_REGS = 7  # 7 bytes
TRACKER_OFF_BORDER = 14  # 1 byte
TRACKER_OFF_READY_FLAG = 15  # 1 byte

# MultiHires tracker (24 bytes at $C700), the hires layout plus a third REC
# family for the 1000-byte color RAM and the bg0 byte for $D021. Same
# single-DMAWRITE packing, same ready flag.
#
#   $C700-$C706 : bitmap REU regs    ($DF02-$DF08 staged values)
#   $C707-$C70D : screen REU regs
#   $C70E-$C714 : color REU regs (dest = $D800, len = 1000)
#   $C715       : bg0 value to write to $D021
#   $C716       : reserved, written 0
#   $C717       : ready flag
#
# The hires and mhires dispatchers share BANK_SWAP_IRQ_HANDLER_ADDR ($C500)
# and FRAME_TRACKER_ADDR ($C700) because they're mutually exclusive (a
# scene only has one display mode at a time).
MHIRES_FRAME_TRACKER_LEN = 24
MHIRES_TRACKER_OFF_BITMAP_REGS = 0  # 7 bytes
MHIRES_TRACKER_OFF_SCREEN_REGS = 7  # 7 bytes
MHIRES_TRACKER_OFF_COLOR_REGS = 14  # 7 bytes
MHIRES_TRACKER_OFF_BG0 = 21  # 1 byte
MHIRES_TRACKER_OFF_RESERVED = 22  # 1 byte
MHIRES_TRACKER_OFF_READY_FLAG = 23  # 1 byte

# Handler-owned state, past the longer tracker so no host frame write reaches
# it. The host cannot know which bank is on screen: it alternates its target
# per pushed frame, but the C64 commits a frame only at a vblank after the
# copy, and a host that stages the next frame first would aim it at the bank
# being displayed and repaint the picture in view. So the dispatcher keeps the
# displayed bank's $DD00 value here, copies into the other bank, and flips
# from this byte rather than from the tracker.
#
# Whether a copied frame is waiting for vblank lives here too, not in the
# ready flag. The host re-arms that flag at its own frame rate, and at 20 fps
# that is about every third field, roughly what an mhires copy plus the wait
# for vblank takes: a re-arm that discarded the copied frame restarted the
# copy so often that the picture updated a few times a second.
#
# And the frame being copied is a snapshot of the tracker, not the tracker.
# The commit writes bg0 and copies color RAM a field or more after the copy
# started, by when the host has usually staged a newer frame: reading those
# from the live tracker put the next frame's colors under this frame's bitmap.
#
# The hires commit writes the border only when it differs from the value it
# last wrote, kept here. The host pokes $D020 itself to show a loop is armed,
# and a commit that rewrote the border every frame would erase that at once.
# The host marks the value stale (bit 7 set) whenever it would have rewritten
# $D020 itself, so the next commit writes it.
BANK_SWAP_STATE_ADDR = 0xC718
assert FRAME_TRACKER_ADDR + MHIRES_FRAME_TRACKER_LEN == BANK_SWAP_STATE_ADDR
_DISPLAYED_BANK = BANK_SWAP_STATE_ADDR  # $DD00 value of the bank on screen
_HIDDEN_BANK_HI = BANK_SWAP_STATE_ADDR + 1  # $80 when the hidden bank is bank 2
_COPIED = BANK_SWAP_STATE_ADDR + 2  # nonzero: the hidden bank holds a frame to show
BORDER_SHOWN_ADDR = BANK_SWAP_STATE_ADDR + 3  # the $D020 value the last commit wrote
BORDER_STALE = 0x80  # bit 7 set: never a color, so the next commit writes its border
_SNAPSHOT = BANK_SWAP_STATE_ADDR + 4  # the copied frame's tracker
BANK_SWAP_STATE_INIT = bytes([CIA2.PORT_A_BANK_0, 0x00, 0x00, BORDER_STALE])
assert BANK_SWAP_STATE_ADDR + len(BANK_SWAP_STATE_INIT) == _SNAPSHOT
BANK_SWAP_STATE_LEN = len(BANK_SWAP_STATE_INIT) + MHIRES_FRAME_TRACKER_LEN

# The audio pump's entry points the merged dispatchers route to.
#
# The bank-swap dispatcher at $C500 sends non-raster IRQs (CIA #1) somewhere.
# Without the REU audio pump that is the kernal at $EA31. With it, the pump
# handler at $C100 (REU_IRQ_HANDLER_TRACKED, for both the video and the mic
# pump) wants every CIA #1 IRQ to run its REU→ring drain, and the two cannot
# both own $0314, so the dispatcher JMPs to $C100 instead. The 6502 can't
# preempt IRQ handlers (I flag), so audio and bank-swap serialize naturally —
# each fully completes its REC ($DF02-$DF08) use before returning. The audio
# handler at $C100 stays byte-for-byte identical (audio_handlers.py owns its
# bytes; this side only routes execution there).
AUDIO_HANDLER_INSTALL_ADDR = REU_PUMP_HANDLER_ADDR  # where audio.AudioStreamer uploads its REU pump
AUDIO_HANDLER_STUB = REU_PUMP_HANDLER_STUB  # JMP $EA31
# What $C180 holds until the audio pump uploads its body there: the merged
# dispatchers JSR $C180 themselves, so without it the first CIA #1 tick that
# latches during a REC family calls whatever an earlier scene or power-on left.
PUMP_BODY_STUB = bytes([0x60])  # RTS


# The raster window gate, shared by every swap handler.
#
# The REU dispatchers need it for their own reason: their in-IRQ copy takes
# several fields, so a flip at the end of it lands at an arbitrary line. They
# copy on one raster IRQ and commit on a later one, through this gate.
#
# The host-DMA handlers need it because of the host's writes.
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
# Out of window the handler acks the IRQ and returns with the frame still
# pending (the host-DMA handlers' ready flag, the REU dispatchers' copied
# flag), so it simply commits on a later field. A deferred
# frame holds the previous one a field longer; it never shows two at once.
#
# A flip is invisible from the IRQ line through RASTER_COMMIT_LAST_SAFE_LINE,
# i.e. $D012 in [251, 255] u [0, 43]; a commit that writes after its flip ends
# earlier (HIRES_COMMIT_LAST_SAFE_LINE, MHIRES_COMMIT_LAST_SAFE_LINE). Adding 5
# rotates that split range into a contiguous 0..48, which is why the check
# costs one compare and one branch instead of two of each.
_RASTER_GATE_BIAS = (0x100 - RASTER_VBLANK_LINE) & 0xFF  # $05
_RASTER_GATE_LIMIT = _RASTER_GATE_BIAS + RASTER_COMMIT_LAST_SAFE_LINE + 1  # $31
assert _RASTER_GATE_LIMIT <= 0xFF

# $D012 is 8 bits and cannot tell line n from line n+256, but every line that
# aliases into the window is below the picture on both systems: NTSC 256-262
# and PAL 256-299 read back as 0-43. PAL 300-311 alias onto 44-55 and are
# conservatively rejected, which only forgoes a commit opportunity. No
# genuinely unsafe line (44-250) can alias into the window,
# since none of them exceed 255. One formulation is correct for PAL and NTSC.


# Chunked REC families.
#
# One REC DMA per family (bitmap = 8000 bytes ≈ 8 ms halt, screen = 1000 ≈
# 1 ms, color = 1000 ≈ 1 ms) loses NMIs. CIA #2 is edge-triggered through the
# NMI line: when a bus halt covers several NMI underflows, the ICR bit latches
# once and the rest collapse into the same edge — losing every NMI past the
# first per halt. Each lost NMI is a sample the reader never advances past, so
# the audio plays slow and flat. That holds for the REU pump's ring and for the
# DAC streamer's alike, since both read through the NMI.
#
# Empirically (2026-05-27 Cam Link D-vs-C diagnosis, 8 kHz): the one-REC mhires
# dispatcher lost ~30 % of NMI events per frame, slowing 8 kHz playback to
# ~5 600 Hz effective.
#
# So each REC goes out in BANK_SWAP_CHUNK_SIZE-byte chunks, so that no halt
# spans an underflow. At 1 cyc/byte the halt is the chunk length, and it has
# to fit inside the SHORTEST NMI period the audio streamer can arm
# (c64.NMI_SAFE_MIN_PERIOD_CYCLES), less the cycles from the halt's end to the
# handler's $DD0D ack — the same budget c64.halt_quantum_bytes sizes the
# host-side ring writes by. A chunk sized for the period at the requested rate
# is the trap: 100 bytes fits the 125-cycle period at 8 kHz but outlasts the
# 85-cycle period of the 12 kHz default, and REU-pump video audio then played
# ~17 % slow against the picture (#661). 50 bytes fits the budget too; 40 is
# what the best U64 run at 12 kHz used, alongside a 32-byte pump chunk (see
# REU_PUMP_CHUNK_SIZE_HEAVY_BUS for the figures, which changed both chunks at
# once). A badline stretches any halt by up to 43 cycles, more than any chunk
# the one-byte counter allows can leave free (below 32 the bitmap family's
# chunk count no longer fits it), so a stretched chunk can still lose a tick;
# a smaller one only leaves less of the stretch past the period.
#
# After each chunk DMA, only the LENGTH register decrements to 0; the src/dst
# registers auto-increment and stay valid across chunks, so the per-chunk
# inner body is just "reload length, retrigger" + the DEC/BNE counter.
#
# With the REU audio pump, CIA #1 loss is partially addressed by per-family
# pump JSR calls. After each family's chunk loop ends, the handler reads $DC0D
# / AND #$01 / BEQ skip / JSR $C180 — picking up any CIA #1 underflow that
# latched into the ICR during the family's halt time. Per-CHUNK pump checks
# would break the REC auto-increment (the pump body overwrites $DF02..$DF06
# with the audio REU/main addresses, so the next chunk would re-trigger a
# transfer from audio → audio rather than the next video slice); the
# per-family check is safe because each family begins with its own
# copy-from-tracker loop that re-sets REC. Without the pump the check is left
# out: reading $DC0D acks the kernal's own jiffy tick, which then never runs.
#
# Zero-page: the chunk counter lives at $FB (the canonical 4-byte user-free
# block $FB-$FE). asid_player.ZP_PTR uses $FB too; the ASID player's own IRQ
# handler owns $0314, so it never runs alongside a bank-swap dispatcher.
BANK_SWAP_CHUNK_SIZE = 40  # bytes per chunked REC DMA
_CHUNK_COUNTER_ZP = 0xFB  # zero-page chunk counter
assert halt_quantum_bytes(NMI_SAFE_MIN_PERIOD_CYCLES) >= BANK_SWAP_CHUNK_SIZE, (
    "a bank-swap chunk's halt must fit inside the shortest NMI period the streamer arms"
)


def _family_source(
    name: str, tracker_addr: int, family_bytes: int, *, banked: bool, pump: bool
) -> str:
    """One REC family: load its registers from the tracker, aim a banked
    family at the hidden bank, then copy it in BANK_SWAP_CHUNK_SIZE chunks.

    The bitmap and screen destinations differ between bank 0 ($2000, $0400)
    and bank 2 ($A000, $8400) only in bit 7 of the high byte, so the handler
    replaces that bit with the hidden bank's rather than trusting the host's.
    """
    chunks, rest = divmod(family_bytes, BANK_SWAP_CHUNK_SIZE)
    assert rest == 0 and 0 < chunks <= 0xFF, (family_bytes, BANK_SWAP_CHUNK_SIZE)
    aim = (
        f"""
            LDA ${tracker_addr + 1:04X}
            AND #$7F
            ORA ${_HIDDEN_BANK_HI:04X}
            STA ${REU.C64_ADDR_HI:04X}
        """
        if banked
        else ""
    )
    tick = (
        f"""
            LDA ${CIA1.ICR:04X}
            AND #$01
            BEQ {name}_done
            JSR ${REU_PUMP_BODY_SUBROUTINE_ADDR:04X}
        {name}_done:
        """
        if pump
        else ""
    )
    return f"""
            LDX #$04
        {name}_regs:
            LDA ${tracker_addr:04X},X
            STA ${REU.C64_ADDR_LO:04X},X
            DEX
            BPL {name}_regs
        {aim}
            LDA #${chunks:02X}
            STA ${_CHUNK_COUNTER_ZP:02X}
        {name}_chunk:
            LDA #${BANK_SWAP_CHUNK_SIZE:02X}
            STA ${REU.LENGTH_LO:04X}
            LDA #$00
            STA ${REU.LENGTH_HI:04X}
            LDA #${REU.CMD_FETCH_EXEC:02X}
            STA ${REU.COMMAND:04X}
            DEC ${_CHUNK_COUNTER_ZP:02X}
            BNE {name}_chunk
        {tick}
    """


def _bank_swap_dispatcher(
    *,
    tracker_len: int,
    hidden: tuple[tuple[int, int], ...],
    after_flip: tuple[tuple[int, int], ...],
    bg0_off: int | None,
    border_off: int | None,
    ready_off: int,
    pump: bool,
    last_line: int,
) -> bytes:
    """Assemble a REU bank-swap dispatcher for $C500.

    A raster IRQ does up to two things, in this order:

    * **Commit** the frame the hidden bank holds, and only while the raster
      gate says the beam is outside the picture: write bg0, flip $DD00 to the
      hidden bank, write the border if it changed, copy the ``after_flip``
      families. Out of the window the
      frame waits for the next field, and nothing is copied over it.
    * **Copy** a staged frame (ready flag set) into the now-hidden bank: clear
      the ready flag, snapshot the tracker, copy the ``hidden`` families, mark
      the bank copied. A host that re-arms during the copy leaves the flag
      set, and its frame is copied after this one is shown.

    Splitting them is what keeps the flip in vblank. The copy takes several
    fields (about 40 ms of chunks for an mhires frame at the 12 kHz default),
    so a flip at the end of it lands wherever the raster happens to be, and
    the picture shows the old frame above that line and the new one below.

    The snapshot is retried until the ready flag is still clear after it: the
    host writes the tracker in one DMA with the flag last, and a DMA landing
    between two of the loop's reads would otherwise leave half of each frame.

    ``hidden`` and ``after_flip`` are one ``(tracker_offset, length)`` per REC
    DMA. A ``hidden`` family is aimed at the hidden bank. ``after_flip`` is
    for color RAM, which is not banked: copying it before the flip would put
    the new colors under the old bitmap for a field, and copying it right
    after the flip outruns the raster — a 40-byte chunk costs ~100 cycles
    with NMIs taken, against the ~500 the beam spends on one 40-cell row —
    so it lands ahead of every row it changes, once the commit starts early
    enough for the first chunk to beat row 0 (``last_line``).

    ``pump`` routes non-raster IRQs to the REU audio pump at $C100 and checks
    for a pending pump tick after each family; without it they chain to the
    kernal.

    ``last_line`` is the last raster line the commit may start on, which a
    commit that writes or copies after the flip has to pull in (see
    HIRES_COMMIT_LAST_SAFE_LINE and MHIRES_COMMIT_LAST_SAFE_LINE).
    """
    assert tracker_len <= MHIRES_FRAME_TRACKER_LEN
    gate_limit = _RASTER_GATE_BIAS + last_line + 1
    assert gate_limit <= _RASTER_GATE_LIMIT, (
        "a commit window cannot reach past RASTER_COMMIT_LAST_SAFE_LINE"
    )
    ready = FRAME_TRACKER_ADDR + ready_off
    nonraster = AUDIO_HANDLER_INSTALL_ADDR if pump else KERNAL.IRQ_HANDLER
    bg0 = f"LDA ${_SNAPSHOT + bg0_off:04X}\n STA $D021" if bg0_off is not None else ""
    border = (
        f"""
            LDA ${_SNAPSHOT + border_off:04X}
            CMP ${BORDER_SHOWN_ADDR:04X}
            BEQ border_done
            STA $D020
            STA ${BORDER_SHOWN_ADDR:04X}
        border_done:
        """
        if border_off is not None
        else ""
    )
    commit_families = "".join(
        _family_source(f"f{i}", _SNAPSHOT + off, n, banked=False, pump=pump)
        for i, (off, n) in enumerate(after_flip)
    )
    copy_families = "".join(
        _family_source(f"h{i}", _SNAPSHOT + off, n, banked=True, pump=pump)
        for i, (off, n) in enumerate(hidden)
    )
    source = f"""
            LDA $D019
            AND #$01
            BNE raster
            JMP ${nonraster:04X}
        raster:
            STA $D019
            LDA ${_COPIED:04X}
            BEQ copy
            LDA $D012
            CLC
            ADC #${_RASTER_GATE_BIAS:02X}
            CMP #${gate_limit:02X}
            BCC commit
            JMP chain
        commit:
            {bg0}
            LDA ${_DISPLAYED_BANK:04X}
            EOR #${CIA2.PORT_A_BANK_0 ^ CIA2.PORT_A_BANK_2:02X}
            STA ${_DISPLAYED_BANK:04X}
            STA ${CIA2.PORT_A:04X}
            {border}
            {commit_families}
            LDA #$00
            STA ${_COPIED:04X}
        copy:
            LDA ${ready:04X}
            BNE snapshot
            JMP chain
        snapshot:
            LDA #$00
            STA ${ready:04X}
            LDX #${tracker_len - 1:02X}
        snapshot_byte:
            LDA ${FRAME_TRACKER_ADDR:04X},X
            STA ${_SNAPSHOT:04X},X
            DEX
            BPL snapshot_byte
            LDA ${ready:04X}
            BNE snapshot
            LDA ${_DISPLAYED_BANK:04X}
            LSR A
            LSR A
            LDA #$00
            ROR A
            STA ${_HIDDEN_BANK_HI:04X}
            {copy_families}
            LDA #$01
            STA ${_COPIED:04X}
        chain:
            JMP ${KERNAL.IRQ_HANDLER:04X}
    """
    return assemble(source, BANK_SWAP_IRQ_HANDLER_ADDR)


# $DD00 bit 1 is set for bank 0 and clear for bank 2, which is what the copy
# phase's LSR/LSR/ROR turns into the hidden bank's bit 7.
assert CIA2.PORT_A_BANK_0 & 0x02 and not CIA2.PORT_A_BANK_2 & 0x02
assert VIC_BANK_2.BITMAP == VIC_BANK_0.BITMAP | 0x8000
assert VIC_BANK_2.SCREEN == VIC_BANK_0.SCREEN | 0x8000


# The last raster line a hires commit may start on. Its border write lands 14
# cycles after the flip and has to beat the picture's first line too, whose
# side border would otherwise show the previous frame's color beside the new
# picture. Under the same worst case as the flip, a commit read on line 43
# wrote the border on line 51. The worst case is computed from these bytes in
# tests/test_commit_window.py.
HIRES_COMMIT_LAST_SAFE_LINE = RASTER_COMMIT_LAST_SAFE_LINE - 1


def _hires_dispatcher(*, pump: bool) -> bytes:
    return _bank_swap_dispatcher(
        tracker_len=FRAME_TRACKER_LEN,
        hidden=(
            (TRACKER_OFF_BITMAP_REGS, REU_VIDEO_BITMAP_LEN),
            (TRACKER_OFF_SCREEN_REGS, REU_VIDEO_BITMAP_SCREEN_LEN),
        ),
        after_flip=(),
        bg0_off=None,
        border_off=TRACKER_OFF_BORDER,
        ready_off=TRACKER_OFF_READY_FLAG,
        pump=pump,
        last_line=HIRES_COMMIT_LAST_SAFE_LINE,
    )


# The last raster line an mhires commit may start on. Its color-RAM copy runs
# after the flip, and the chunk holding cell row 0's colors has to land before
# row 0's badline (51) fetches them, or that frame shows the new bitmap under
# the previous frame's colors in the top row. From the raster read to the end
# of that chunk the handler spends about 165 cycles of its own; audio NMIs at
# the fastest rate the streamer arms take over half the CPU on top of that,
# and one audio-ring write's DMA halt can land in it. That comes to about 12
# PAL lines, so a commit read on line 45 finished the chunk around line 57. The
# worst case is computed from these bytes in tests/test_commit_window.py.
MHIRES_COMMIT_LAST_SAFE_LINE = 38


def _mhires_dispatcher(*, pump: bool) -> bytes:
    return _bank_swap_dispatcher(
        tracker_len=MHIRES_FRAME_TRACKER_LEN,
        hidden=(
            (MHIRES_TRACKER_OFF_BITMAP_REGS, REU_VIDEO_BITMAP_LEN),
            (MHIRES_TRACKER_OFF_SCREEN_REGS, REU_VIDEO_BITMAP_SCREEN_LEN),
        ),
        after_flip=((MHIRES_TRACKER_OFF_COLOR_REGS, REU_VIDEO_BITMAP_COLOR_LEN),),
        bg0_off=MHIRES_TRACKER_OFF_BG0,
        border_off=None,
        ready_off=MHIRES_TRACKER_OFF_READY_FLAG,
        pump=pump,
        last_line=MHIRES_COMMIT_LAST_SAFE_LINE,
    )


BANK_SWAP_IRQ_HANDLER = _hires_dispatcher(pump=False)
MHIRES_BANK_SWAP_IRQ_HANDLER = _mhires_dispatcher(pump=False)
BANK_SWAP_CHUNKED_PLUS_AUDIO_IRQ_HANDLER = _hires_dispatcher(pump=True)
MHIRES_BANK_SWAP_CHUNKED_PLUS_AUDIO_IRQ_HANDLER = _mhires_dispatcher(pump=True)


# Host-DMA double-buffer swap IRQ handler (no-REU backends, e.g. TeensyROM).
#
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
# the bitmap render crisply. NMI audio lives on
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
        _RASTER_GATE_BIAS,  # 19 ADC #$05         ; 251..255 → 0..4, 0..43 → 5..48
        0xC9,
        _RASTER_GATE_LIMIT,  # 21 CMP #$31
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


# Flicker blend ([color].flicker_tolerance) — page-flip every field.
#
# The host-DMA sibling above, plus an unconditional per-field toggle of the
# $D018 screen-matrix nibble between the two page offsets (c64.D018_HIRES_PAGE_A
# / _B). Two screen pages holding different color nibbles over one shared
# bitmap therefore alternate at the VIC field rate, and the eye fuses each cell's
# pair into a color the VIC cannot draw. See video/flicker.py for which pairs
# are eligible and why.
#
# The toggle is deliberately ahead of the ready-flag check, and ahead of the
# raster gate: the alternation is the C64's job and must free-run at the field
# rate whatever the host is doing, which is the whole reason this does not need
# 50-60 fps over the link. Gating it would drop fields out of the fusion cadence
# — a worse artifact than a late page flip, which only mistimes the blended
# cells' colors rather than showing two frames of bitmap at once. Only the
# double-buffer commit ($DD00 + $D021) waits on a staged frame and a safe raster.
#
# That commit is additionally gated on landing in phase 0, so a bank swap can
# never transpose the A/B page roles — without it a swap arriving on an odd
# field would put field A's nibbles on field B's slot for the rest of the scene,
# which is invisible on a still frame and reads as a color shift on motion.
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
        _RASTER_GATE_BIAS,  # 37 ADC #$05         ; 251..255 → 0..4, 0..43 → 5..48
        0xC9,
        _RASTER_GATE_LIMIT,  # 39 CMP #$31
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


# Also in c64.CIA2, kept here as Python ints rather than strings for the
# per-frame push.
DD00_BANK_0 = CIA2.PORT_A_BANK_0  # $97
DD00_BANK_2 = CIA2.PORT_A_BANK_2  # $95

# CIA #1 ICR control words for raster-IRQ bring-up / teardown (see CIA1).
_CIA1_ICR_DISABLE_TIMER_A = CIA1.ICR_DISABLE_ALL
_CIA1_ICR_ENABLE_TIMER_A = CIA1.ICR_ENABLE_TIMER_A


def wait_out_reu_copy() -> None:
    """Wait as long as a REU dispatcher's copy can run from $C500 once both IRQ
    sources are masked. Waiting is cheaper than a handshake, which would need a
    REST read of the C64 on every scene change."""
    time.sleep(_REU_SLOT_MAX_IN_USE_S)


def mask_irq_sources(api: C64Backend, *, drain_reu_copy: bool = False) -> None:
    """Mask CIA #1 and the VIC IRQ sources (raster + sprite collisions +
    light pen), so no IRQ enters whatever $0314 names while it is overwritten.

    `drain_reu_copy` then waits out a copy a leaked REU dispatcher may have in
    flight: the masks keep new IRQs out of $C500, but one already inside a copy
    keeps writing a VIC bank for fields at a time. The double-buffer setups
    pass it before they clear both banks and pin bank 0, so neither the clear
    nor the pin is undone behind them."""
    api.write_memory(f"{CIA1.ICR:04X}", f"{_CIA1_ICR_DISABLE_TIMER_A:02X}")
    api.write_memory("D01A", "00")
    if drain_reu_copy:
        wait_out_reu_copy()


def install_bank_swap_irq(
    api: C64Backend,
    handler_bytes: bytes = BANK_SWAP_IRQ_HANDLER,
    tracker_len: int = FRAME_TRACKER_LEN,
    *,
    audio_pump_active: bool = False,
    tracker_init: bytes | None = None,
) -> None:
    """Bring up the bank-swap raster IRQ.

    `handler_bytes` and `tracker_len` default to the hires dispatcher and
    its 16-byte tracker. MultiHires passes its own (24-byte tracker), and the
    host-DMA page flips theirs. All of them live at the same addresses
    (BANK_SWAP_IRQ_HANDLER_ADDR, FRAME_TRACKER_ADDR) because a scene has one
    display mode at a time. The REU dispatchers' state is seeded too, to
    bank 0, which the callers pin $DD00 to first.

    `audio_pump_active`: True when the scene also opted into REU audio
    (`use_reu_pump = true`). In that case `handler_bytes` is expected to
    be a merged dispatcher (BANK_SWAP_CHUNKED_PLUS_AUDIO_IRQ_HANDLER
    or the mhires equivalent) whose non-raster branch JMPs to $C100 where the
    audio pump handler lives. We pre-upload a 3-byte JMP $EA31 stub at
    $C100 and a lone RTS at $C180 (the pump-body subroutine the merged
    dispatchers JSR) BEFORE hooking $0314, so the gap between this
    install completing (CIA #1 IRQ re-enabled at the end) and the audio
    streamer uploading the real pump is covered by a safe fall-through
    instead of a jump into uninitialized RAM or a previous scene's pump.
    Nothing else replaces those stubs: a scene whose audio never starts
    keeps them, and its CIA #1 ticks reach the kernal and pump nothing.

    Order matters: mask both raster and CIA #1 sources before anything is
    uploaded (`mask_irq_sources`), then hook $0314, program the raster
    compare line, ack any pending raster IRQ, and enable raster + re-enable
    CIA #1. A teardown whose $0314 restore never landed leaves the vector on
    $C500, so an IRQ taken while the upload below is half done would run a
    half-written handler. Same sequence as
    [overlays/big_text.py:_install_raster_irq]."""
    tracker = bytes(tracker_len) if tracker_init is None else tracker_init
    if len(tracker) != tracker_len:
        raise ValueError(f"tracker_init must be {tracker_len} bytes, got {len(tracker)}")
    mask_irq_sources(api)
    if audio_pump_active:
        # The stubs must be in place before CIA #1 is re-enabled at the end of
        # this function, and before $0314 names the merged dispatcher, whose
        # non-raster branch jumps to $C100.
        api.write_memory_file(f"{REU_PUMP_BODY_SUBROUTINE_ADDR:04X}", PUMP_BODY_STUB)
        api.write_memory_file(f"{AUDIO_HANDLER_INSTALL_ADDR:04X}", AUDIO_HANDLER_STUB)
    api.write_memory_file(f"{BANK_SWAP_IRQ_HANDLER_ADDR:04X}", handler_bytes)
    # Ready flag (last byte) = 0, so the first IRQ after install skips the DMA
    # path until the host stages a real frame.
    #
    # `tracker_init` overrides those zeros for handlers with a field the IRQ
    # *reads* unconditionally rather than only behind the ready flag — the
    # flicker handler's $D018 page pair. Zeros there would point VIC at the
    # $0000 matrix offset for the field or two before the first frame stages,
    # so the seed has to be in place before the $D01A write below arms the
    # raster source.
    api.write_memory_file(f"{FRAME_TRACKER_ADDR:04X}", tracker)
    # The REU dispatchers flip from this byte, and setup has pinned $DD00 to
    # bank 0, so it has to say bank 0 before the first commit. The host-DMA
    # handlers flip from their tracker and never read it.
    api.write_memory_file(f"{BANK_SWAP_STATE_ADDR:04X}", BANK_SWAP_STATE_INIT)
    # write_regs packs both vector bytes into one DMA, so $0314/$0315 is never
    # half-updated on the wire.
    api.write_regs(
        f"{VECTORS.IRQ:04X}",
        BANK_SWAP_IRQ_HANDLER_ADDR & 0xFF,
        (BANK_SWAP_IRQ_HANDLER_ADDR >> 8) & 0xFF,
    )
    # RASTER_VBLANK_LINE = 251 is the first line below the picture, so the bank
    # swap lands after the final row's last bitmap fetch. $D011 bit 7 is the
    # raster MSB, left 0 (lines 0-255 only).
    api.write_memory("D012", f"{RASTER_VBLANK_LINE:02X}")
    # Ack any latent raster flag before enabling the raster IRQ source.
    api.write_memory("D019", "01")
    api.write_memory("D01A", "01")
    # Re-enable the CIA #1 jiffy IRQ — kernal keyboard scan etc.
    api.write_memory(f"{CIA1.ICR:04X}", f"{_CIA1_ICR_ENABLE_TIMER_A:02X}")


def uninstall_bank_swap_irq(api: C64Backend, *, drain_reu_copy: bool = True) -> None:
    """Tear down the bank-swap raster IRQ. Mirror of install_bank_swap_irq
    in reverse, plus restore $DD00 = bank 0 so the next scene's setup
    sees the kernal-default VIC bank.

    The unhook is `hw/irq_unhook.unhook_raster_irq`, whose docstring has the
    order and the retry rules; the VIC bank 0 write runs between its ack and
    its CIA #1 unmask, confirmed delivered.

    `drain_reu_copy` waits out a REU dispatcher's in-flight copy between the
    masks and the vector restore. A caller whose installed handler is the
    host-DMA or flicker swap, which copies nothing, passes False and skips
    the wait."""
    unhook_raster_irq(
        api,
        log,
        "bank-swap IRQ",
        drain=wait_out_reu_copy if drain_reu_copy else None,
        before_unmask=(
            # Restore VIC bank 0 (kernal default) so the next scene paints into
            # the addresses it expects.
            (
                "VIC bank 0",
                lambda: confirm(
                    api,
                    "VIC bank 0",
                    lambda: api.write_memory(f"{CIA2.PORT_A:04X}", f"{DD00_BANK_0:02X}"),
                ),
            ),
        ),
    )


def _rec_regs(c64_dest: int, reu_src: int, length: int) -> bytes:
    """One family's 7 staged REC bytes, $DF02-$DF08 order."""
    return bytes(
        [
            c64_dest & 0xFF,
            (c64_dest >> 8) & 0xFF,
            reu_src & 0xFF,
            (reu_src >> 8) & 0xFF,
            (reu_src >> 16) & 0xFF,
            length & 0xFF,
            (length >> 8) & 0xFF,
        ]
    )


def _stage_bitmap_and_screen(
    api: C64Backend, bitmap_bytes: bytes, screen_bytes: bytes, slot: int
) -> tuple[int, bytes]:
    """REUWRITE bitmap + screen into staging slot ``slot`` (bus-clean — no C64
    halt). Returns the slot's REU offset and the two families' 14 tracker
    bytes, aimed at bank 0 (the IRQ re-aims them)."""
    offset = (slot % REU_VIDEO_SLOTS) * REU_VIDEO_SLOT_STRIDE
    api.reu_write(REU_VIDEO_BITMAP_BASE + offset, bitmap_bytes)
    api.reu_write(REU_VIDEO_BITMAP_SCREEN_BASE + offset, screen_bytes)
    regs = _rec_regs(
        VIC_BANK_0.BITMAP, REU_VIDEO_BITMAP_BASE + offset, REU_VIDEO_BITMAP_LEN
    ) + _rec_regs(
        VIC_BANK_0.SCREEN, REU_VIDEO_BITMAP_SCREEN_BASE + offset, REU_VIDEO_BITMAP_SCREEN_LEN
    )
    return offset, regs


def push_bitmap_via_reu(
    api: C64Backend, bitmap_bytes: bytes, screen_bytes: bytes, border: int, slot: int
) -> None:
    """REUWRITE bitmap + screen into REU staging slot ``slot``, then DMAWRITE
    the 16-byte frame tracker to $C700-$C70F. The C64-side raster IRQ copies
    the frame into whichever VIC bank is hidden and flips $DD00 at a later
    vblank — all without any further host involvement.

    ``slot`` is the caller's rotation through REU_VIDEO_SLOTS (see the REU
    staging layout). The destinations are bank 0's; the IRQ re-aims them.
    ``border`` is the frame's $D020 value, which the IRQ writes as it flips.

    Per-frame host work: 2 REUWRITEs (bus-clean) + 1 DMAWRITE (16 bytes,
    halts C64 bus for ~16 cycles — negligible vs the ~9000 cycles the
    REU→main DMAs themselves consume)."""
    offset, regs = _stage_bitmap_and_screen(api, bitmap_bytes, screen_bytes, slot)
    # Order matches the IRQ handler's layout exactly, and the ready flag is the
    # LAST byte, so the regs are consistent before ready flips.
    tracker = regs + bytes([border & 0x0F, 0x01])  # border, ready flag
    assert len(tracker) == FRAME_TRACKER_LEN
    api.write_memory_file(f"{FRAME_TRACKER_ADDR:04X}", tracker)


def push_mhires_via_reu(
    api: C64Backend,
    bitmap_bytes: bytes,
    screen_bytes: bytes,
    color_bytes: bytes,
    bg0: int,
    slot: int,
) -> None:
    """MultiHires bank-swap push. Extends push_bitmap_via_reu with a third
    REUWRITE for the 1000-byte color RAM, plus a bg0 byte in the tracker
    that the IRQ writes to $D021 as it flips.

    Per-frame host work: 3 REUWRITEs (bus-clean) + 1 DMAWRITE (24 bytes,
    halts C64 bus ~24 cycles — negligible). The big halts (bitmap ~8000,
    screen ~1000, color ~1000 = ~10000 cycles total) happen on the C64
    side, triggered by the raster IRQ."""
    offset, regs = _stage_bitmap_and_screen(api, bitmap_bytes, screen_bytes, slot)
    api.reu_write(REU_VIDEO_BITMAP_COLOR_BASE + offset, color_bytes)
    # Order matches the IRQ handler's layout exactly, and the ready flag is the
    # LAST byte, so the regs are consistent whenever the handler sees ready=1.
    tracker = (
        regs
        + _rec_regs(
            SCREEN.COLOR_RAM, REU_VIDEO_BITMAP_COLOR_BASE + offset, REU_VIDEO_BITMAP_COLOR_LEN
        )
        + bytes([bg0 & 0xFF, 0x00, 0x01])  # bg0, reserved, ready flag
    )
    assert len(tracker) == MHIRES_FRAME_TRACKER_LEN
    api.write_memory_file(f"{FRAME_TRACKER_ADDR:04X}", tracker)


def push_screen_via_reu(api: C64Backend, screen_bytes: bytes, dest_addr: int) -> None:
    """REUWRITE the screen bytes to REU, then trigger a REU→main DMA into
    `dest_addr` (the screen RAM location for the current VIC bank — $0400
    for bank 0, $8400 for bank 2). Used by the REU-staged char-mode push.
    Each frame is a one-shot transfer (no auto-increment across triggers),
    so the REU source offset stays pinned at REU_VIDEO_SCREEN_BASE — the
    REUWRITE overwrites the staging area each frame."""
    # Stage the new screen into REU SRAM (clean — no C64 bus halt).
    api.reu_write(REU_VIDEO_SCREEN_BASE, screen_bytes)
    # write_regs packs contiguous register writes into one DMA command, so the
    # REU regs go in 3 commands instead of 7. Addr-control is auto-inc both,
    # which is the default 0.
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
    # The CPU halts for ~1000 cycles (1 byte/cycle) while the REU→main DMA
    # copies the staged frame into screen RAM. The only bus-halt event in the
    # REU-staged char push.
    api.write_memory(f"{REU.COMMAND:04X}", f"{REU.CMD_FETCH_EXEC:02X}")


def reu_pump_skips_irq_hook(display_mode: object) -> bool:
    """Whether the REU audio pump starting under `display_mode` must leave
    $0314 alone and run tracked: True when the mode installs a merged
    bank-swap dispatcher that calls the $C100 pump itself.

    Every pump start asks it: VideoScene.setup (start_for_reu_staged), and
    WebcamScene.setup, BlankScene.setup and MicAudioSource.setup (start_mic).

    Raises ValueError for a mode that drives the REC from the host
    (`drives_rec_from_host`), which no pump variant can share it with (see
    REU_VIDEO_SCREEN_BASE). scene_factory.resolve_use_reu_staged keeps such a
    mode from being built while [audio].use_reu_pump is on, so this is only
    reached by a mode built some other way."""
    if getattr(display_mode, "drives_rec_from_host", False):
        raise ValueError(
            f"{type(display_mode).__name__} pushes its screen through the REU "
            "from the host, which the REU audio pump cannot share; build it "
            "with use_reu_staged=False while [audio].use_reu_pump is on"
        )
    return bool(
        getattr(display_mode, "audio_reu_pump_active", False)
        and getattr(display_mode, "use_reu_staged", False)
    )
