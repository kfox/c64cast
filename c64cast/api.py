"""Client for the Ultimate 64.

Two transports, used for orthogonal sets of operations:

  * **Socket DMA** ([socket_dma.py](socket_dma.py)) on TCP port 64 carries
    every memory write — `write_memory`, `write_memory_file`, `write_regs`,
    `write_region`. The connection is persistent and the wire format is a
    4-byte header + payload, so per-write cost is ~5 ms (vs ~14 ms over
    REST). See [docs/caveats.md](../docs/caveats.md) → "Socket DMA
    replaced HTTP for writes" for the history and benchmark.

  * **REST** (`requests.Session`) on port 80 carries everything DMA can't:
    `read_memory` (GET), `reset` (PUT), `run_basic_clear_loop` and
    `run_sid_player` (POST /v1/runners:run_prg), and the startup `probe`
    (GET /). Low frequency, latency not critical.

The two transports run independently and don't share state. `flush()`
synchronizes the DMA pipeline against subsequent REST calls (e.g. before
`reset` or `run_sid_player`) by issuing a trailing DMA IDENTIFY round-
trip; by the FIFO guarantee of the U64's per-connection command loop,
the IDENTIFY reply lands only after every prior DMAWRITE has executed.

`run_sid_player` deliberately avoids `/v1/runners:sidplay` because that
endpoint takes over HDMI with the firmware's own SID-player UI, blocking
any other visualization. Instead we DMA the SID payload + a ~30-byte
6502 player into C64 RAM and POST a tiny BASIC SYS stub via `run_prg`;
the real 6510 then executes INIT once and PLAY at IRQ time, chaining to
the kernal at $EA31 so keyboard scan + cursor suppression survive.

Delta uploads (`write_region`) cache the last-pushed bytes per region and
push only the changed sub-range or chunked diffs — applies to both DMA
and REST eras since it sits above the transport.
"""

from __future__ import annotations

import logging
import os
import time
from abc import abstractmethod
from dataclasses import dataclass
from typing import NamedTuple
from urllib.parse import urlparse

import requests

from .backend import (
    ULTIMATE_PROFILE,
    BufferedWriteBackend,
    HardwareProfile,
)
from .c64 import CIA1, CPU, ROM, U64_API, VECTORS
from .socket_dma import DEFAULT_PORT, SocketDMAClient, SocketDMAError

__all__ = ["Ultimate64API", "SocketDMAError", "ParsedPsid", "parse_psid_for_player"]

log = logging.getLogger(__name__)

# Tokenized BASIC for `10 PRINT CHR$(147) : 20 GOTO 20` as a PRG file
# (2-byte load address $0801 prefix, then linked-list of BASIC lines,
# terminated by 00 00). PRINT=$99, CHR$=$C7, GOTO=$89.
BASIC_CLEAR_LOOP_PRG = bytes(
    [
        0x01,
        0x08,  # load address $0801
        0x0D,
        0x08,  # line 10 next-line ptr = $080D
        0x0A,
        0x00,  # line number 10
        0x99,  # PRINT
        0xC7,  # CHR$
        0x28,
        0x31,
        0x34,
        0x37,
        0x29,  # (147)
        0x00,  # end of line
        0x16,
        0x08,  # line 20 next-line ptr = $0816
        0x14,
        0x00,  # line number 20
        0x89,  # GOTO
        0x20,
        0x32,
        0x30,  # " 20"
        0x00,  # end of line
        0x00,
        0x00,  # end of program
    ]
)

# C64-side SID player. Default base $C300 (just past audio.py's
# $C000-$C2FF allocation for the NMI DAC + REU pump handlers); per-tune
# relocated by [_choose_player_layout] when the SID payload would overlap
# the default. 61 bytes; the IRQ handler entry sits at base + 38.
#
# CPU-port ($01) banking is PER-CALL, mirroring the U64's own SID player
# (firmware software/6502/sidcrt/player.asm): the player RESTS at $37
# (BASIC + KERNAL + I/O all mapped — the standard environment most tunes
# assume) and only switches the bank TRANSIENTLY around each routine call,
# restoring $37 immediately after:
#   init: LDA #initBank / STA $01 / JSR init / LDA #$37 / STA $01
#   play: LDA #playBank / STA $01 / JSR play / LDA #$37 / STA $01  (per IRQ)
# initBank (slot _SID_PATCH_INITBANK) and playBank (slot _SID_PATCH_PLAYBANK)
# are computed by [_init_bank_for]/[_play_bank_for] via the getBank rule
# ($Dx→$34, ≥$E0→$35, ≥$A0→$36, else $37): init from the load-END page,
# play from the play-address page. So a tune under BASIC ROM (e.g. Hyperion 2
# at $AE2A) runs init/play under $36 (reaching its RAM, not the ROM's
# SYNTAX-error stub at $AF08), while a tune that reads BASIC ROM as a data
# table (e.g. Election) gets the $37 resting environment everywhere except
# the brief banked window. An EARLIER design set $01 once and never restored
# it; leaving BASIC permanently banked out crashed tunes like Election (Matt
# Gray) ~24 s in — hard enough to wedge the whole U64 — because their code
# assumes the $37 resting state between PLAY calls.
#
# IRQ handler shape: `JSR play` then a tick divider — every N ticks the
# handler chains to the kernal IRQ tail at $EA31 (SCNKEY / UDTIM /
# cursor blink); the other N-1 ticks take a lean exit (`LDA $DC0D` to
# ack CIA #1, then `JMP $EA81` for the kernal's register-restore RTI).
# Without the divider, fast-PLAY tunes (Wizball at ~151 Hz; anything
# whose INIT reprograms CIA #1 Timer A below ~$3000) run SCNKEY +
# UDTIM + blink on every tick and waste 20-30% of CPU on kernal
# overhead, audibly distorting the player. N is patched in live by
# [Ultimate64API._tune_play_divider] after INIT settles. Default N=1
# in the template = unchanged behavior (chain on every tick) until
# the host has measured the actual PLAY rate.
#
# After installing the IRQ vector the main thread spins in a tight
# `JMP *` rather than RTSing back to BASIC: many SID INITs clobber
# zero-page locations BASIC depends on (text pointers, evaluator state),
# so returning to BASIC's `GOTO 20` loop reliably triggers a syntax/
# illegal-quantity error visible on screen. Spinning here is harmless —
# the kernal's CIA #1 Timer A IRQ keeps firing, so PLAY runs at IRQ time
# and `$028D` keeps updating for the keyboard poller (every N-th tick).
SID_PLAYER_MC_ADDR = 0xC300

# Offsets within the player MC of the address-bearing instructions and
# state bytes that other code points at:
#  * IRQ_HANDLER  — target of $0314/15 (start of `JSR play / divider / ...`)
#  * SPIN         — JMP <spin> own operand, so the CPU loops on the JMP
#  * COUNTER      — 1-byte live tick counter, decremented in the IRQ
#  * DIVIDER      — the LDA #N immediate inside the reload sequence;
#                   _tune_play_divider patches this byte in place
# Address slots are derived as player_base + the OFFSET constants.
SID_PLAYER_IRQ_HANDLER_OFFSET = 42
SID_PLAYER_SPIN_OFFSET = 39
SID_PLAYER_COUNTER_OFFSET = 72
SID_PLAYER_DIVIDER_OFFSET = 59

# Patch offsets into the player MC template. Three flavors:
#  * Per-tune operands (song / init / play) — filled from ParsedPsid.
#  * Internal references (irq / spin / counter address) — filled from the
#    chosen layout's player_base + the OFFSET constants above.
#  * Tick divider N — seeded to 1 in the template; live-patched by
#    [Ultimate64API._tune_play_divider] after INIT.
# Bytes start at 0x00 (or 0x01 for the divider seed + counter) in the
# template so an unpatched address operand is obviously broken on use.
_SID_PATCH_INITBANK = 2  # LDA #<initBank> $01 value around JSR init
#              (see [_init_bank_for])
_SID_PATCH_SONG = 6  # LDA #song-1
_SID_PATCH_INIT_LO = 12  # JSR init operand low
_SID_PATCH_INIT_HI = 13  # JSR init operand high
_SID_PATCH_CTR_INIT_LO = 26  # STA counter (init seed) operand low
_SID_PATCH_CTR_INIT_HI = 27  # STA counter (init seed) operand high
_SID_PATCH_IRQ_LO = 29  # LDA #<irq_handler  (immediate operand)
_SID_PATCH_IRQ_HI = 34  # LDA #>irq_handler  (immediate operand)
_SID_PATCH_SPIN_LO = 40  # JMP <spin> operand low
_SID_PATCH_SPIN_HI = 41  # JMP <spin> operand high
_SID_PATCH_PLAYBANK = 43  # LDA #<playBank> $01 value around JSR play
#              (see [_play_bank_for])
_SID_PATCH_PLAY_LO = 47  # JSR play operand low  (inside IRQ handler)
_SID_PATCH_PLAY_HI = 48  # JSR play operand high
_SID_PATCH_CTR_DEC_LO = 54  # DEC counter operand low
_SID_PATCH_CTR_DEC_HI = 55  # DEC counter operand high
_SID_PATCH_DIVIDER = 59  # LDA #N immediate operand (live-patched)
_SID_PATCH_CTR_RELOAD_LO = 61  # STA counter (reload) operand low
_SID_PATCH_CTR_RELOAD_HI = 62  # STA counter (reload) operand high
#
# Bank-config history (don't repeat past experiments):
#   2026-05-26: tried `LDA #$36 / STA $01` (unmap BASIC ROM) between SEI
#   and JSR init UNCONDITIONALLY, hoping to fix the Comic Bakery silent-
#   after-INIT symptom (plays a brief INIT beep on this player MC but
#   plays fine via the U64 firmware's `/v1/runners:sidplay` endpoint).
#   Result: Comic Bakery still broken, Wizball unchanged, Last Ninja 2
#   regressed (crashed to READY after a couple of notes). Lesson: $36 is
#   wrong as a one-size-fits-all — tunes like Comic Bakery deliberately
#   read BASIC ROM as a data table and need it mapped ($37).
#   2026-05-29: made the bank value PER-TUNE (one $01 set once at startup):
#   under-BASIC-ROM tunes got $36, others $37. This played Hyperion 2 but
#   left BASIC permanently banked out for the $36 tunes.
#   2026-06-09: that permanent bank CRASHED tunes like Election (Matt Gray)
#   ~24 s in — wedging the whole U64 — because their code assumes the $37
#   resting environment between PLAY calls, and the $36/$37 choice for the
#   "data under ROM, entry points in RAM" class proved undecidable offline
#   (Election needs $37, Sunday_Night needs $36, both look identical). The
#   fix matches the U64's own player: bank PER-CALL (see [_bank_for_addr_hi],
#   [_init_bank_for], [_play_bank_for]) — rest at $37, switch to initBank
#   around JSR init and playBank around JSR play, restore $37 after each.
#   KERNAL-underlay tunes ($E000+) are still refused upfront in
#   [parse_psid_for_player] (banking KERNAL out kills the $EA31 IRQ chain).
#
# The `LDA #$0F / STA $D418` after JSR init restores the SID master volume
# nibble. Two scenarios make it necessary:
#  1. An earlier audio.stop() zeroed $D418 for a clean video cutoff —
#     PSID INIT routines conventionally don't touch $D418 (they assume the
#     host already set it to $0F), so without this restore the SID would
#     run with PLAY writing voice registers but master volume stuck at 0,
#     producing total silence on the U64's HDMI feed.
#  2. Some PSID INITs DO write $D418 to reset state, often to zero — this
#     restore happens AFTER INIT returns so it can't be wiped.
# Running between INIT and the IRQ install means the kernal IRQ can't fire
# mid-restore (we're still under the SEI at the entry point).
SID_PLAYER_MC_TEMPLATE = bytes(
    [
        # --- init (offsets 0-41) -------------------------------------------
        0x78,  # 00  SEI
        0xA9,
        0x37,  # 01  LDA #<initBank>  (CPU port $01 around
        #               JSR init; $37 default, $36
        #               under BASIC ROM — patched
        #               by _init_bank_for)
        0x85,
        0x01,  # 03  STA $01   (transient bank so JSR init
        #               reaches the tune's RAM)
        0xA9,
        0x00,  # 05  LDA #song-1            (patched)
        0xA2,
        0x00,  # 07  LDX #$00
        0xA0,
        0x00,  # 09  LDY #$00
        0x20,
        0x00,
        0x00,  # 11  JSR init_addr          (patched)
        0xA9,
        0x37,  # 14  LDA #$37   (restore the resting bank:
        #               BASIC+KERNAL+I/O mapped, the
        #               environment tunes assume
        #               between calls)
        0x85,
        0x01,  # 16  STA $01
        0xA9,
        0x0F,  # 18  LDA #$0F   (master volume max)
        0x8D,
        0x18,
        0xD4,  # 20  STA $D418
        0xA9,
        0x01,  # 23  LDA #$01   (seed counter = 1 so the
        #               first IRQ chains + reloads
        #               with whatever N the host
        #               has patched by then)
        0x8D,
        0x00,
        0x00,  # 25  STA counter            (patched)
        0xA9,
        0x00,  # 28  LDA #<irq_handler      (patched)
        0x8D,
        0x14,
        0x03,  # 30  STA $0314
        0xA9,
        0x00,  # 33  LDA #>irq_handler      (patched)
        0x8D,
        0x15,
        0x03,  # 35  STA $0315
        0x58,  # 38  CLI
        0x4C,
        0x00,
        0x00,  # 39  JMP <spin>             (patched —
        #               points at itself; don't
        #               return to corrupted BASIC)
        # --- IRQ handler entry @ offset 42 -------------------------------
        0xA9,
        0x37,  # 42  LDA #<playBank>  (CPU port $01 around
        #               JSR play — patched by
        #               _play_bank_for)
        0x85,
        0x01,  # 44  STA $01
        0x20,
        0x00,
        0x00,  # 46  JSR play_addr          (patched)
        0xA9,
        0x37,  # 49  LDA #$37   (restore resting bank after
        #               play, before the kernal tail)
        0x85,
        0x01,  # 51  STA $01
        0xCE,
        0x00,
        0x00,  # 53  DEC counter            (patched)
        0xD0,
        0x08,  # 56  BNE lean_exit (+8 -> offset 66)
        0xA9,
        0x01,  # 58  LDA #N   (divider, live-patched by
        #              _tune_play_divider; seeded
        #              to 1 = chain every tick
        #              until measured)
        0x8D,
        0x00,
        0x00,  # 60  STA counter            (patched)
        0x4C,
        0x31,
        0xEA,  # 63  JMP $EA31  (kernal IRQ tail:
        #              SCNKEY + UDTIM + blink)
        # --- lean exit @ offset 66 ----------------------------------------
        0xAD,
        0x0D,
        0xDC,  # 66  LDA $DC0D  (ack CIA #1 IRQ — read
        #              clears the flag; skipping it
        #              would re-fire immediately)
        0x4C,
        0x81,
        0xEA,  # 69  JMP $EA81  (kernal register
        #              restore + RTI)
        # --- counter byte @ offset 72 -------------------------------------
        0x01,  # 72  counter (live: decremented per IRQ;
        #     reloaded to N on underflow)
    ]
)

# SHIFT-driven subtune cycling. Default base $C400 (clean page boundary
# past the player MC at $C300-$C33C, with headroom for both regions to
# grow); per-tune relocated alongside the player by [_choose_player_layout].
# `cue_song_reinit(song)` DMA-patches the song byte at _REINIT_PATCH_SONG,
# then DMA-swaps $0314/$0315 to point here. The very next kernal IRQ tick
# (≤16ms NTSC / ≤20ms PAL) runs the stub once, which calls INIT on the
# new song, restores the SID master volume nibble, restores $0314/$0315
# back to the regular PLAY handler, and chains to $EA31. Subsequent IRQ
# ticks resume normal PLAY on the new subtune.
#
# Like the main player MC, the stub banks PER-CALL: `LDA #<initBank> /
# STA $01` (patched by [_init_bank_for]) so a BASIC-ROM-underlay tune's
# re-INIT reaches RAM, then `LDA #$37 / STA $01` after JSR init to restore
# the resting environment before handing control back to the PLAY handler
# (which does its own per-call playBank).
#
# No SEI/CLI: the kernal IRQ entry has already disabled IRQs before
# vectoring through $0314. INIT runs with IRQs masked, same as it would
# under the main player MC's initial SEI/CLI bracket.
REINIT_STUB_ADDR = 0xC400

# Patch offsets. Per-tune operands filled by [_build_reinit_stub] at
# upload; the song byte is then re-patched in place by [cue_song_reinit]
# each SHIFT.
_REINIT_PATCH_BANK = 1  # LDA #<initBank> $01 value around JSR init
_REINIT_PATCH_SONG = 5  # LDA #song-1
_REINIT_PATCH_INIT_LO = 11  # JSR init operand low
_REINIT_PATCH_INIT_HI = 12  # JSR init operand high
_REINIT_PATCH_IRQ_LO = 23  # LDA #<play handler (immediate operand)
_REINIT_PATCH_IRQ_HI = 28  # LDA #>play handler (immediate operand)

REINIT_STUB_TEMPLATE = bytes(
    [
        0xA9,
        0x37,  # 00  LDA #<initBank>  (patched; transient
        #               bank around JSR init)
        0x85,
        0x01,  # 02  STA $01
        0xA9,
        0x00,  # 04  LDA #song-1        (patched)
        0xA2,
        0x00,  # 06  LDX #$00
        0xA0,
        0x00,  # 08  LDY #$00
        0x20,
        0x00,
        0x00,  # 10  JSR init_addr      (patched)
        0xA9,
        0x37,  # 13  LDA #$37   (restore resting bank)
        0x85,
        0x01,  # 15  STA $01
        0xA9,
        0x0F,  # 17  LDA #$0F           (master volume max)
        0x8D,
        0x18,
        0xD4,  # 19  STA $D418
        0xA9,
        0x00,  # 22  LDA #<play handler (patched)
        0x8D,
        0x14,
        0x03,  # 24  STA $0314
        0xA9,
        0x00,  # 27  LDA #>play handler (patched)
        0x8D,
        0x15,
        0x03,  # 29  STA $0315
        0x4C,
        0x31,
        0xEA,  # 32  JMP $EA31          (chain kernal IRQ)
    ]
)

# Audio handler region — audio.py installs NMI DAC at $C020 and REU pump
# handlers at $C100-$C2FF. Refuse player layouts that would overlap so
# we don't clobber bytes the audio path may read/write under us.
_AUDIO_REGION_LO = 0xC000
_AUDIO_REGION_HI = 0xC300  # exclusive

# Highest legal end address for the player bundle. $D000+ is I/O space.
_PLAYER_BUNDLE_HI_MAX = 0xD000

# Lowest legal player base. The BASIC SYS stub lives at $0801-$0811, with
# the same $0820 margin parse_psid_for_player applies to load_addr.
_PLAYER_BASE_MIN = 0x0820

# Stub-from-player offset used when the player is relocated past its
# default position. 80 bytes clears the 73-byte player MC (7 bytes spare)
# while keeping the relocated bundle small enough to slot into modest free
# holes.
_RELOCATED_STUB_OFFSET = 80


@dataclass(frozen=True)
class _PlayerLayout:
    """Resolved on-C64 addresses for one SID-player upload.

    `player_base` is where SID_PLAYER_MC_TEMPLATE lands; `stub_base` is
    where REINIT_STUB_TEMPLATE lands. The internal references inside
    the player MC (IRQ handler entry, spin-loop target, counter byte,
    divider byte) are derived from `player_base` + the OFFSET constants
    — exposed as properties so the patching helpers don't recompute them
    inline."""

    player_base: int
    stub_base: int

    @property
    def irq_handler_addr(self) -> int:
        return self.player_base + SID_PLAYER_IRQ_HANDLER_OFFSET

    @property
    def spin_addr(self) -> int:
        return self.player_base + SID_PLAYER_SPIN_OFFSET

    @property
    def counter_addr(self) -> int:
        return self.player_base + SID_PLAYER_COUNTER_OFFSET

    @property
    def divider_addr(self) -> int:
        return self.player_base + SID_PLAYER_DIVIDER_OFFSET


_DEFAULT_PLAYER_LAYOUT = _PlayerLayout(player_base=SID_PLAYER_MC_ADDR, stub_base=REINIT_STUB_ADDR)


def _patch_word(buf: bytearray, lo_off: int, hi_off: int, addr: int) -> None:
    """Patch a 16-bit C64 address into `buf` at the given byte offsets.
    Used for both contiguous JSR/JMP operands (lo_off + 1 = hi_off) and
    split LDA-imm pairs where the high byte's operand sits a few bytes
    after the low byte's."""
    buf[lo_off] = addr & 0xFF
    buf[hi_off] = (addr >> 8) & 0xFF


def _layout_fits(
    layout: _PlayerLayout, parsed: ParsedPsid, avoid: bytes | bytearray | None = None
) -> bool:
    """True when the layout's player + stub blocks both land in legal
    free RAM (above $0820, below $D000), don't overlap audio.py's
    $C000-$C2FF region, don't overlap the SID payload, don't overlap
    each other, and (when `avoid` is given) don't overlap any RAM byte the
    tune writes / the caller reserved.

    `avoid` is an optional 64 KB bitmap (1 = occupied) — the union of the
    tune's observed RAM write footprint and the caller's scene-reserved
    regions. See [ram_write_footprint](sid_host_emu.py) and the
    scene-reserved regions assembled in WaveformScene.setup."""
    payload_lo = parsed.load_addr
    payload_hi = parsed.load_addr + len(parsed.payload)
    blocks = (
        (layout.player_base, len(SID_PLAYER_MC_TEMPLATE)),
        (layout.stub_base, len(REINIT_STUB_TEMPLATE)),
    )
    for base, size in blocks:
        end = base + size
        if base < _PLAYER_BASE_MIN or end > _PLAYER_BUNDLE_HI_MAX:
            return False
        if base < _AUDIO_REGION_HI and end > _AUDIO_REGION_LO:
            return False
        if base < payload_hi and end > payload_lo:
            return False
        if avoid is not None and any(avoid[base:end]):
            return False
    p_base, p_size = blocks[0]
    s_base, s_size = blocks[1]
    return not (p_base < s_base + s_size and s_base < p_base + p_size)


def _find_free_layout(parsed: ParsedPsid, avoid: bytes | bytearray) -> _PlayerLayout:
    """Place the player bundle in the LARGEST contiguous RAM hole the tune
    never writes (and the caller didn't reserve).

    `avoid` is the union of the tune's observed write footprint and the
    scene-reserved regions. We scan $0820-$D000 for runs of bytes that are
    free of `avoid`, the SID payload, and audio.py's $C000-$C2FF region,
    and pick the largest such run that can hold the 115-byte bundle (player
    MC 73 + re-INIT stub at player_base+80). Largest-first (tie-break
    lowest address) puts the player deep in genuinely-unused RAM, which
    both fixes scratch-near-payload tunes (e.g. Beat_Dis writes the page
    right after its payload) and leaves margin against patterns a finite
    footprint sample didn't reach.

    Raises ValueError if no hole is large enough.
    """
    bundle_size = _RELOCATED_STUB_OFFSET + len(REINIT_STUB_TEMPLATE)
    payload_lo = parsed.load_addr
    payload_hi = parsed.load_addr + len(parsed.payload)

    def _blocked(addr: int) -> bool:
        if payload_lo <= addr < payload_hi:
            return True
        if _AUDIO_REGION_LO <= addr < _AUDIO_REGION_HI:
            return True
        return bool(avoid[addr])

    # Collect every free run in [_PLAYER_BASE_MIN, _PLAYER_BUNDLE_HI_MAX).
    runs: list[tuple[int, int]] = []  # (start, end_exclusive)
    addr = _PLAYER_BASE_MIN
    while addr < _PLAYER_BUNDLE_HI_MAX:
        if _blocked(addr):
            addr += 1
            continue
        start = addr
        while addr < _PLAYER_BUNDLE_HI_MAX and not _blocked(addr):
            addr += 1
        runs.append((start, addr))

    # Largest run first; tie-break on lowest start for determinism.
    runs.sort(key=lambda r: (-(r[1] - r[0]), r[0]))
    for start, end in runs:
        if end - start >= bundle_size:
            return _PlayerLayout(player_base=start, stub_base=start + _RELOCATED_STUB_OFFSET)

    raise ValueError(
        f"no free slot for the SID player: payload "
        f"${payload_lo:04X}-${payload_hi:04X} plus the tune's RAM "
        f"footprint leave no {bundle_size}-byte hole in $0820-$CFFF"
    )


def _choose_player_layout(
    parsed: ParsedPsid, avoid: bytes | bytearray | None = None
) -> _PlayerLayout:
    """Pick on-C64 addresses for the player MC + re-INIT stub.

    Always tries the historical default ($C300 / $C400) first. When that
    doesn't fit:
      * with `avoid` (the tune's RAM write footprint ∪ scene-reserved
        regions): place the bundle in the largest footprint-clean hole via
        [_find_free_layout] — robust against tunes that use RAM adjacent to
        their payload as scratch.
      * without `avoid` (legacy callers): fall back to the old
        adjacent-to-payload heuristic (page just past, then just below the
        payload). Kept for backward compatibility; the footprint path is
        strictly better and is what WaveformScene uses.
    Raises ValueError if no candidate slot is free.
    """
    if _layout_fits(_DEFAULT_PLAYER_LAYOUT, parsed, avoid):
        return _DEFAULT_PLAYER_LAYOUT

    if avoid is not None:
        return _find_free_layout(parsed, avoid)

    def _relocated(base: int) -> _PlayerLayout:
        return _PlayerLayout(player_base=base, stub_base=base + _RELOCATED_STUB_OFFSET)

    payload_hi = parsed.load_addr + len(parsed.payload)
    payload_lo = parsed.load_addr
    bundle_size = _RELOCATED_STUB_OFFSET + len(REINIT_STUB_TEMPLATE)

    # First fallback: page-aligned just past the SID payload, bumped up
    # past audio's region if it landed inside.
    above = (payload_hi + 0xFF) & ~0xFF
    if above < _AUDIO_REGION_HI:
        above = _AUDIO_REGION_HI
    candidate = _relocated(above)
    if _layout_fits(candidate, parsed):
        return candidate

    # Second fallback: page-aligned just below the SID payload.
    below = (payload_lo - bundle_size) & ~0xFF
    candidate = _relocated(below)
    if _layout_fits(candidate, parsed):
        return candidate

    raise ValueError(
        f"no free slot for the SID player: payload "
        f"${payload_lo:04X}-${payload_hi:04X} blocks the default "
        f"$C300/$C400 layout and both relocation candidates"
    )


def _bank_for_addr_hi(hi: int) -> int:
    """6510 CPU port ($01) value for running code/data whose page high-byte
    is `hi`. Mirrors the U64 firmware's getBank (sidcommon.asm):

      $Dx        -> $34  (all-RAM, I/O out: RAM under $Dxxx is reachable)
      >= $E0     -> $35  (KERNAL ROM banked out; I/O kept)
      >= $A0     -> $36  (BASIC ROM banked out; KERNAL + I/O kept)
      otherwise  -> $37  (default: BASIC + KERNAL + I/O all mapped)

    The SID player uses this per-call (init from the load-END page, play from
    the play page) and restores $37 between calls; see SID_PLAYER_MC_TEMPLATE.
    KERNAL-underlay tunes ($E000+) never reach here — parse_psid_for_player
    refuses them upfront (banking KERNAL out would kill the $EA31 IRQ chain).
    RSIDs are likewise refused; the U64's RSID->$37 branch is therefore moot.
    """
    if hi & 0xF0 == 0xD0:
        return CPU.PORT_IO_OUT
    if hi >= (ROM.KERNAL_LO >> 8):
        return CPU.PORT_KERNAL_OUT
    if hi >= (ROM.BASIC_LO >> 8):
        return CPU.PORT_BASIC_OUT
    return CPU.PORT_DEFAULT


def _init_bank_for(parsed: ParsedPsid) -> int:
    """Bank for the JSR init call: from the LOAD-END page (the U64 keys init
    banking on the load-end address, so a tune whose data spans into ROM
    space runs init with that space mapped as RAM)."""
    end_hi = (parsed.load_addr + len(parsed.payload) - 1) >> 8
    return _bank_for_addr_hi(end_hi)


def _play_bank_for(parsed: ParsedPsid) -> int:
    """Bank for the per-IRQ JSR play call: from the play-address page."""
    return _bank_for_addr_hi(parsed.play_addr >> 8)


def _build_player_mc(
    parsed: ParsedPsid, layout: _PlayerLayout, play_bank: int | None = None
) -> bytes:
    mc = bytearray(SID_PLAYER_MC_TEMPLATE)
    mc[_SID_PATCH_INITBANK] = _init_bank_for(parsed)
    # play_bank override: the static heuristic keys on the play *address*
    # page, but a tune can read its live song data from RAM under BASIC ROM
    # (e.g. Galway's Times of Lore subtunes 2-11 read $B400) while its code
    # sits below $A000. The caller detects that from the PLAY footprint and
    # passes $36 so PLAY sees RAM there instead of ROM. See WaveformScene.
    mc[_SID_PATCH_PLAYBANK] = play_bank if play_bank is not None else _play_bank_for(parsed)
    mc[_SID_PATCH_SONG] = (parsed.song_to_play - 1) & 0xFF
    _patch_word(mc, _SID_PATCH_INIT_LO, _SID_PATCH_INIT_HI, parsed.init_addr)
    _patch_word(mc, _SID_PATCH_PLAY_LO, _SID_PATCH_PLAY_HI, parsed.play_addr)
    _patch_word(mc, _SID_PATCH_IRQ_LO, _SID_PATCH_IRQ_HI, layout.irq_handler_addr)
    _patch_word(mc, _SID_PATCH_SPIN_LO, _SID_PATCH_SPIN_HI, layout.spin_addr)
    # All three counter-address operands point at the same byte (the
    # counter at counter_addr); patched together so a layout relocation
    # can't desync them.
    counter = layout.counter_addr
    _patch_word(mc, _SID_PATCH_CTR_INIT_LO, _SID_PATCH_CTR_INIT_HI, counter)
    _patch_word(mc, _SID_PATCH_CTR_DEC_LO, _SID_PATCH_CTR_DEC_HI, counter)
    _patch_word(mc, _SID_PATCH_CTR_RELOAD_LO, _SID_PATCH_CTR_RELOAD_HI, counter)
    return bytes(mc)


def _build_reinit_stub(parsed: ParsedPsid, layout: _PlayerLayout) -> bytes:
    stub = bytearray(REINIT_STUB_TEMPLATE)
    stub[_REINIT_PATCH_BANK] = _init_bank_for(parsed)
    stub[_REINIT_PATCH_SONG] = (parsed.song_to_play - 1) & 0xFF
    _patch_word(stub, _REINIT_PATCH_INIT_LO, _REINIT_PATCH_INIT_HI, parsed.init_addr)
    _patch_word(stub, _REINIT_PATCH_IRQ_LO, _REINIT_PATCH_IRQ_HI, layout.irq_handler_addr)
    return bytes(stub)


def _build_basic_sys_stub(sys_addr: int) -> bytes:
    """Tokenized BASIC PRG: `10 SYS <decimal sys_addr>`. The SID-player
    BASIC stub is a single-line program; supplying the address here
    lets the player be relocated per-tune without touching the template.

    On-disk PRG layout: 2-byte load-address header ($0801), one BASIC
    line (next-line ptr, line number, tokens, end-of-line null), then
    two terminating null bytes that flag end-of-program."""
    LOAD_ADDR = 0x0801
    sys_tokens = bytes([0x9E, 0x20])  # SYS, ' '
    digits = str(sys_addr).encode("ascii")
    line_body = sys_tokens + digits + b"\x00"  # ... + end-of-line
    # next_line_ptr = where the *following* line's next-line ptr field
    # would start = load_addr + 2 (skip own ptr field) + 2 (line num) + body.
    next_line_ptr = LOAD_ADDR + 4 + len(line_body)
    return (
        bytes([LOAD_ADDR & 0xFF, (LOAD_ADDR >> 8) & 0xFF])
        + next_line_ptr.to_bytes(2, "little")
        + bytes([0x0A, 0x00])  # line number 10
        + line_body
        + bytes([0x00, 0x00])
    )  # end of program


class ParsedPsid(NamedTuple):
    """A PSID file post-validation, ready to drive both the C64-side player
    and the host-side py65 emulator. `payload` has any inline load-address
    header bytes already consumed, so `payload[0]` is the byte that goes at
    `load_addr` on the C64. `song_to_play` is 1-based and bounds-checked
    against `num_songs`."""

    load_addr: int
    init_addr: int
    play_addr: int
    num_songs: int
    start_song: int
    song_to_play: int
    payload: bytes


def parse_psid_for_player(sid_bytes: bytes, song: int = 0) -> ParsedPsid:
    """Parse + validate a PSID for the kernal-chained player path used by
    [Ultimate64API.run_sid_player](api.py) and the host-side SidHostEmu.

    Shared so the C64-side player and the host emulator both reject the
    same set of unsupported tunes — keeps WaveformScene errors consistent
    regardless of which side surfaces them.

    Raises ValueError on:
      * Magic != PSID (RSID is called out specifically).
      * load_addr inside the BASIC stub window ($0801-$081F).
      * play_addr == 0 (INIT installs its own IRQ — incompatible with
        kernal IRQ chaining).
      * payload/init/play reaching into KERNAL ROM ($E000-$FFFF) — the
        player keeps KERNAL mapped to chain the $EA31 IRQ tail, so it
        can't bank KERNAL out to expose RAM there. (BASIC-ROM-underlay
        tunes at $A000-$BFFF ARE supported: the player banks BASIC out
        per-call around init/play — see [_bank_for_addr_hi].)
      * song out of range 1..num_songs.
    """
    if len(sid_bytes) < 22:
        raise ValueError("SID file too short to contain a header")
    magic = sid_bytes[:4]
    if magic == b"RSID":
        raise ValueError(
            "RSID tunes are not supported by run_sid_player — they "
            "expect their own raster IRQ and don't cooperate with "
            "the kernal-chained player. Use a PSID-format tune."
        )
    if magic != b"PSID":
        raise ValueError(f"not a SID file (expected PSID/RSID magic, got {magic!r})")
    data_offset = int.from_bytes(sid_bytes[6:8], "big")
    load_addr = int.from_bytes(sid_bytes[8:10], "big")
    init_addr = int.from_bytes(sid_bytes[10:12], "big")
    play_addr = int.from_bytes(sid_bytes[12:14], "big")
    num_songs = int.from_bytes(sid_bytes[14:16], "big")
    start_song = int.from_bytes(sid_bytes[16:18], "big")
    # If load_addr is 0, the first 2 bytes of the data payload carry the
    # real load address (PSID v1+ convention).
    payload = sid_bytes[data_offset:]
    if load_addr == 0:
        load_addr = payload[0] | (payload[1] << 8)
        payload = payload[2:]
    if init_addr == 0:
        init_addr = load_addr
    if play_addr == 0:
        raise ValueError(
            "SID has play_addr=0 (INIT installs its own IRQ); "
            "run_sid_player only supports tunes with an explicit "
            "PLAY entry point."
        )
    # The BASIC stub at $0801 occupies 17 bytes ($0801-$0811 — the
    # tokenized `10 SYS 49920` plus the 2-byte load-address header). A
    # SID whose payload starts inside that window would be clobbered
    # when /v1/runners:run_prg loads the stub. Threshold rounded up to
    # $0820 for safety margin.
    if load_addr < 0x0820:
        raise ValueError(
            f"SID load_addr ${load_addr:04X} conflicts with the BASIC "
            f"SYS stub at $0801-$0811 — choose a tune that loads at "
            f"$0820 or higher."
        )
    # Tunes whose code/data live under KERNAL ROM ($E000-$FFFF) can't be
    # played: the kernal-chained player keeps KERNAL mapped (to JMP $EA31
    # at IRQ time), so banking it out to expose that RAM isn't an option.
    # BASIC-ROM-underlay tunes ($A000-$BFFF) are fine — the player banks
    # BASIC out per-call (see [_bank_for_addr_hi]) while leaving KERNAL + I/O
    # mapped.
    payload_hi = load_addr + len(payload)
    kernal_spans = [
        (load_addr, payload_hi),
        (init_addr, init_addr + 1),
        (play_addr, play_addr + 1),
    ]
    for lo, hi in kernal_spans:
        if lo < ROM.KERNAL_HI and hi > ROM.KERNAL_LO:
            raise ValueError(
                f"SID has code/data under KERNAL ROM "
                f"(payload ${load_addr:04X}-${payload_hi:04X}, "
                f"init ${init_addr:04X}, play ${play_addr:04X}; "
                f"overlaps $E000-$FFFF) — the kernal-chained player keeps "
                f"KERNAL mapped for its $EA31 IRQ tail and can't expose "
                f"RAM there. Unsupported."
            )
    song_to_play = song if song > 0 else start_song
    if song_to_play < 1 or song_to_play > num_songs:
        raise ValueError(f"song {song_to_play} out of range 1..{num_songs}")
    return ParsedPsid(
        load_addr=load_addr,
        init_addr=init_addr,
        play_addr=play_addr,
        num_songs=num_songs,
        start_song=start_song,
        song_to_play=song_to_play,
        payload=payload,
    )


class _SidPlayerBackend(BufferedWriteBackend):
    """SID-player orchestration shared by the Ultimate + TeensyROM backends.

    The host-side work — PSID parse, player-layout choice, player MC / re-INIT
    stub build, the CIA #1 PLAY-rate divider auto-tune, and SHIFT-driven subtune
    re-INIT (`cue_song_reinit`) — is identical across backends: it touches only
    the buffered write path + `read_memory` + the module-level SID helpers. The
    backends differ in exactly one step: how the BASIC SYS stub that hands
    control to the player MC is delivered to the C64. That step is the abstract
    `_launch_sid_player`:

      * Ultimate — POST a `10 SYS <player_base>` PRG to the REST `run_prg`
        runner (a soft reset that preserves RAM, then RUN).
      * TeensyROM — LaunchFile a pre-uploaded constant stub that SYSes a small
        trampoline (PostFile is menu-gated, so it can't upload a per-tune stub
        mid-stream; see teensyrom_api.TeensyROMBackend).

    Both real backends set `profile.supports_run_prg = True`; this mixin
    overrides the ABC's capability-gated (raising) `run_sid_player` /
    `cue_song_reinit` with the working implementation.
    """

    def __init__(self) -> None:
        super().__init__()
        # Set by run_sid_player; consumed by cue_song_reinit so SHIFT-driven
        # song cycling patches the stub at the same address the player MC
        # was uploaded to. None until the first run_sid_player call.
        self._sid_player_layout: _PlayerLayout | None = None
        # The address-keyed heuristic playBank for the current tune (constant
        # across its subtunes — play_addr doesn't change per song). cue_song_
        # reinit restores it when a cycle target needs no override, so a prior
        # subtune's $36 override can't leak into a $37 subtune.
        self._sid_player_default_play_bank: int | None = None
        # Wall-clock instant the real SID began playing (set by run_sid_player
        # when audio starts synchronously, or by begin_sid_audio when deferred).
        # Exposed via sid_audio_start_time() for the scope's host-emu clock.
        self._sid_audio_start: float | None = None
        # True between a run_sid_player(defer_audio=True) and the matching
        # begin_sid_audio() on backends that can defer (the TeensyROM); guards
        # begin_sid_audio against a double-start / a stray call.
        self._sid_audio_pending = False

    # ---- backend-specific kick (subclass implements) ----------------------
    @abstractmethod
    def _launch_sid_player(
        self,
        parsed: ParsedPsid,
        layout: _PlayerLayout,
        mc: bytes,
        reinit: bytes,
        timeout: float,
        avoid: bytes | bytearray | None,
        defer_audio: bool,
    ) -> bool:
        """DMA the SID payload + player MC + re-INIT stub into C64 RAM and hand
        control to the player MC. Use `_write_sid_blobs` for the standard
        three-blob upload. `avoid` is the caller's RAM footprint bitmap (or
        None), forwarded for backends that need it.

        Returns True to have `run_sid_player` run the standard post-start
        finalize — record the audio-start instant + auto-tune the PLAY-rate
        divider — used by a backend that starts audio synchronously right here
        (the Ultimate's `run_prg`). Returns False if the backend manages that
        itself: either the start is deferred to `begin_sid_audio()`, or the
        backend self-finalizes after its own kick (the TeensyROM, whose `$0314`
        vector-swap must precede the divider's CIA #1 read, so it can't let
        `run_sid_player` finalize before the swap)."""
        ...

    def _write_sid_blobs(
        self, parsed: ParsedPsid, layout: _PlayerLayout, mc: bytes, reinit: bytes
    ) -> None:
        """DMA the SID payload + patched player MC + re-INIT stub to their C64
        addresses. Invalidates the delta cache first (the payload + player MC
        overlap arbitrary RAM regions; a clean baseline keeps the next scene's
        writes diffing against fresh state). Does NOT flush — the caller flushes
        once all blobs (plus any backend-specific extras, e.g. the TR
        trampoline) have been queued, so they all land before the BASIC SYS
        fires."""
        self.invalidate_cache()
        self.write_memory_file(f"{parsed.load_addr:04X}", parsed.payload)
        self.write_memory_file(f"{layout.player_base:04X}", mc)
        self.write_memory_file(f"{layout.stub_base:04X}", reinit)

    def run_sid_player(
        self,
        sid_bytes: bytes,
        song: int = 0,
        timeout: float = 5.0,
        *,
        avoid: bytes | bytearray | None = None,
        play_bank: int | None = None,
        defer_audio: bool = False,
    ) -> None:
        """Play a SID on the real 6510 without going through the firmware's
        own SID-player UI.

        `play_bank` overrides the CPU-port ($01) value used transiently around
        the per-IRQ JSR play. Pass $36 (BASIC ROM out) for tunes whose PLAY
        reads live data from RAM under BASIC ROM ($A000-$BFFF) even though their
        code sits below it — the address-keyed heuristic can't see that, but the
        caller's PLAY footprint can. None = use the heuristic ([_play_bank_for]).

        Sequence:
          1. Parse the PSID/RSID header for load/init/play addresses. Refuse
             RSIDs, tunes loading below $0820 (would collide with the BASIC SYS
             stub), tunes with play_addr 0, and code/data under KERNAL ROM.
          2. Choose where to place the player MC + re-INIT stub. Default is
             $C300/$C400; relocates per-tune when the SID payload would overlap
             (see [_choose_player_layout]). Pass `avoid` (a 64 KB bitmap of RAM
             the tune writes ∪ the caller's reserved regions) to relocate into
             the largest footprint-clean hole.
          3-5. DMA the payload + player MC + re-INIT stub, then hand control to
             the player via the backend-specific `_launch_sid_player`. The player
             banks $01 per-call, calls INIT once, installs a $0314 IRQ that calls
             PLAY then chains to kernal $EA31, then spins forever in `JMP *` (so
             the kernal IRQ keeps firing PLAY + updating $028D for the keyboard
             poller; returning to BASIC would syntax-error on INIT-clobbered ZP).
          6. Measure the post-INIT CIA #1 Timer A rate and patch the player MC's
             kernal-chain divider so fast-PLAY tunes don't run SCNKEY every tick.

        `song` is the 1-based subtune; pass 0 to use the SID's default.

        `defer_audio=True` loads the player but leaves it silent until
        `begin_sid_audio()` — WaveformScene uses it to bring the oscilloscope up
        before the first note. A backend that can't defer (the Ultimate's
        synchronous `run_prg`) starts immediately and ignores the flag.

        v1 limitations: PSID only; PAL/NTSC speed flag ignored (kernal-default
        CIA #1 rate). See [docs/caveats.md] for the full rationale.
        """
        parsed = parse_psid_for_player(sid_bytes, song=song)
        layout = _choose_player_layout(parsed, avoid)
        self._sid_player_layout = layout
        self._sid_player_default_play_bank = _play_bank_for(parsed)

        if layout is not _DEFAULT_PLAYER_LAYOUT:
            log.info(
                "SID player relocated to player=$%04X stub=$%04X "
                "(default $C300/$C400 conflicts with payload "
                "$%04X-$%04X)",
                layout.player_base,
                layout.stub_base,
                parsed.load_addr,
                parsed.load_addr + len(parsed.payload),
            )

        mc = _build_player_mc(parsed, layout, play_bank=play_bank)
        reinit = _build_reinit_stub(parsed, layout)
        finalize = self._launch_sid_player(parsed, layout, mc, reinit, timeout, avoid, defer_audio)

        if finalize:
            # The backend started audio synchronously here (the Ultimate's
            # run_prg) — anchor the host-emu clock and, once INIT has reprogrammed
            # CIA #1 Timer A, measure the PLAY rate and patch the tick divider. A
            # backend that self-finalizes or defers (the TeensyROM) returns False
            # and owns this itself (in begin_sid_audio / its own kick).
            self._sid_audio_start = time.time()
            self._tune_play_divider()

    def begin_sid_audio(self) -> None:
        """Release a SID start deferred by `run_sid_player(defer_audio=True)`.

        The base implementation is a no-op: the only backend that defers is the
        TeensyROM (which DMA-swaps `$0314` to the re-INIT stub here); the
        Ultimate starts audio synchronously inside `_launch_sid_player` and never
        reaches a deferred state."""
        return

    def sid_audio_start_time(self) -> float | None:
        return self._sid_audio_start

    def cue_song_reinit(self, song: int, *, play_bank: int | None = None) -> None:
        """Cue the next kernal IRQ tick to re-INIT the SID on a new subtune,
        without going through the BASIC-runs-SYS-stub path. Avoids the runner
        round-trip that resets VIC mode + clears screen RAM, so SHIFT-driven
        song cycling in WaveformScene stays flicker-free.

        Requires `run_sid_player` to have been called first — it picks the
        per-tune player layout and uploads the re-INIT stub at the layout's
        stub_base.
        Sequence:
          1. DMA-patch the song operand at stub_base + _REINIT_PATCH_SONG.
          2. When `play_bank` is given (or restore the tune default otherwise),
             DMA-patch the player MC's playBank operand so PLAY of the new
             subtune uses the right $01 value. The player MC isn't rebuilt on a
             cue, so a subtune that needs a different bank than the start song
             would otherwise keep the start song's bank and play silent.
          3. Atomically DMA-swap $0314/$0315 to point at the stub.
          4. The next kernal IRQ runs the stub: JSR init(new song), restore
             $D418=$0F, restore $0314/$0315 back to the regular PLAY handler,
             JMP $EA31. Subsequent IRQs resume PLAY on the new subtune.
        `song` is the 1-based subtune number.
        """
        layout = self._sid_player_layout
        if layout is None:
            raise RuntimeError(
                "cue_song_reinit called before run_sid_player — the "
                "re-INIT stub hasn't been uploaded yet"
            )
        self.write_memory(
            f"{layout.stub_base + _REINIT_PATCH_SONG:04X}", f"{(song - 1) & 0xFF:02X}"
        )
        # Patch the player MC's playBank BEFORE the vector swap so the first
        # PLAY after the re-INIT stub restores the vector already uses it.
        # When the caller passes None, restore the tune's heuristic default
        # so a previous subtune's override (e.g. $36 for a Times-of-Lore
        # under-ROM subtune) doesn't leak into one that wants $37.
        bank = play_bank if play_bank is not None else self._sid_player_default_play_bank
        if bank is not None:
            self.write_memory(
                f"{layout.player_base + _SID_PATCH_PLAYBANK:04X}", f"{bank & 0xFF:02X}"
            )
        self.write_regs(
            f"{VECTORS.IRQ:04X}", layout.stub_base & 0xFF, (layout.stub_base >> 8) & 0xFF
        )
        # The new subtune's INIT may reprogram CIA #1 Timer A to a different
        # rate — re-measure and re-patch the tick divider. Longer settle than
        # run_sid_player's path: cue takes effect on the NEXT kernal IRQ, then
        # the stub runs INIT, then we want to observe the post-INIT latch.
        self._tune_play_divider(settle_s=0.08)

    # CIA #1 Timer A latch sampling for [_tune_play_divider].
    # Reads are 1 byte each, and the timer counts down from latch to 0 then
    # reloads. Eight reads span enough time to catch a value within ~10% of the
    # latch even at the highest PLAY rates we care about.
    _DIVIDER_LATCH_SAMPLES = 8

    # Target kernal-services rate. SCNKEY at >= 30 Hz keeps $028D updating fast
    # enough for the 10 Hz keyboard poller.
    _DIVIDER_TARGET_KERNAL_HZ = 30

    # Cap the divider so a misread (very high estimated PLAY rate) can't starve
    # kernal services entirely.
    _DIVIDER_MAX = 8

    # PHI2 approximation in Hz. PAL is 985248, NTSC is 1022730 — using 1e6
    # introduces <2% error, well within the rounding tolerance.
    _DIVIDER_PHI2_HZ = 1_000_000

    def _tune_play_divider(self, settle_s: float = 0.2) -> int:
        """Sample CIA #1 Timer A to estimate the SID's PLAY rate, then
        live-patch the player MC's tick divider so the kernal IRQ tail (SCNKEY +
        UDTIM + cursor blink at $EA31) only runs every Nth PLAY tick.

        Returns the patched N (1 = chain every tick = legacy behavior).
        Best-effort: a read or write failure logs and returns 1 without raising.

        Works on any backend whose `read_memory` reaches CIA #1 — the Ultimate
        (REST) and cycle-clean TeensyROM (ReadC64Mem) both do. A backend that
        can't read returns None from read_memory and the divider stays at the
        template default (N=1), which is correct but leaves fast-PLAY tunes
        running the kernal tail every tick.
        """
        layout = self._sid_player_layout
        if layout is None:
            return 1
        # Settle so INIT has had a chance to reprogram CIA #1 Timer A.
        time.sleep(settle_s)
        # The CIA latch is write-only at $DC04/$DC05; reading those returns the
        # current down-count. Max over a small window catches a fresh reload.
        max_count = 0
        for _ in range(self._DIVIDER_LATCH_SAMPLES):
            buf = self.read_memory(CIA1.TIMER_A_LO, 2)
            if buf is None or len(buf) < 2:
                log.debug(
                    "_tune_play_divider: CIA1 read failed; leaving divider at template default"
                )
                return 1
            v = buf[0] | (buf[1] << 8)
            if v > max_count:
                max_count = v
        if max_count == 0:
            log.debug(
                "_tune_play_divider: CIA1 latch sampled as 0; leaving divider at template default"
            )
            return 1
        play_rate_hz = self._DIVIDER_PHI2_HZ / max_count
        divider = max(1, int(play_rate_hz / self._DIVIDER_TARGET_KERNAL_HZ))
        if divider > self._DIVIDER_MAX:
            divider = self._DIVIDER_MAX
        try:
            self.write_memory(f"{layout.divider_addr:04X}", f"{divider & 0xFF:02X}")
            self.flush()
        except Exception:
            log.warning(
                "_tune_play_divider: failed to patch divider byte at $%04X",
                layout.divider_addr,
                exc_info=True,
            )
            return 1
        log.info(
            "SID player: CIA1 latch~=$%04X (~%.0fHz PLAY) -> "
            "kernal-chain divider=%d (~%.0fHz service rate)",
            max_count,
            play_rate_hz,
            divider,
            play_rate_hz / divider,
        )
        return divider


class Ultimate64API(_SidPlayerBackend):
    def __init__(
        self,
        base_url: str,
        *,
        dma_port: int = DEFAULT_PORT,
        dma_password: str | None = None,
        profile: HardwareProfile | None = None,
    ):
        # Init the shared write path (delta cache, stats, listeners) + the
        # SID-player state fields (_sid_player_layout / _default_play_bank).
        super().__init__()
        # The Ultimate is fully capable; default to the generic Ultimate
        # profile when constructed directly (tests, doctor). make_backend()
        # passes a profile with the NTSC/PAL-resolved default_fps.
        self.profile = profile if profile is not None else ULTIMATE_PROFILE
        self.base_url = base_url.rstrip("/")
        self.read_url = f"{self.base_url}{U64_API.READ_MEM}"
        self.reset_url = f"{self.base_url}{U64_API.RESET}"
        self.timeout = 0.5

        self.session = requests.Session()

        # Socket DMA transport for writes. urlparse extracts the bare host
        # from the REST base URL so we don't need a second config field —
        # they're the same physical box.
        host = urlparse(self.base_url).hostname
        if not host:
            raise ValueError(f"could not extract hostname from {base_url!r}")
        self.socket_dma = SocketDMAClient(host=host, port=dma_port, password=dma_password)
        # connect() raises SocketDMAError on refused/auth-rejected; let it
        # propagate so the CLI can render a user-actionable message.
        self.socket_dma.connect()

    # ---- write path (DMA) ---------------------------------------------------
    _EMIT_WRITE_LABEL = "U64 dma write"
    _EMIT_DEVICE_LABEL = "U64"

    def _emit(self, addr: int, payload: bytes) -> None:
        """Route a write through Socket DMA. On OSError or SocketDMAError
        (server died completely, reconnect failed, or mid-handshake
        IDENTIFY/auth round-trip didn't reply), the shared failure ladder
        logs on an escalating schedule so the user eventually sees a problem
        even without -vv, but never raises — a transient network issue
        shouldn't crash the playlist. The next call retries the reconnect."""
        try:
            self.socket_dma.dmawrite(addr, payload)
            self._stats["writes"] += 1
            self._note_emit_success()
        except (OSError, SocketDMAError) as e:
            self._note_emit_failure(addr, e)

    # ---- read / runner / reset (REST) --------------------------------------
    def read_memory(self, address: int, length: int, timeout: float = 1.0) -> bytes | None:
        """Read `length` bytes from the U64. Returns None on failure.

        REST GET — Socket DMA has no read opcode. Cheap enough for 10 Hz
        polling of small ranges (e.g. the Commodore-key poller reads 1
        byte at $028D)."""
        try:
            r = self.session.get(
                self.read_url,
                params={"address": f"{address:04X}", "length": str(length)},
                timeout=timeout,
            )
            r.raise_for_status()
            return r.content
        except requests.RequestException as e:
            log.debug("read_memory %04X failed: %s", address, e)
            return None

    def run_basic_clear_loop(self, timeout: float = 5.0) -> None:
        """Upload and run a tiny BASIC program: `10 PRINT CHR$(147) : 20 GOTO 20`.

        `PRINT CHR$(147)` clears + homes the screen, and the infinite
        `GOTO 20` loop keeps BASIC out of the editor's direct-input mode
        so the kernal cursor blink stays suppressed for free. Call right
        after `reset()` so the BASIC READY banner is wiped before the
        first scene paints.
        """
        self.flush()
        self.invalidate_cache()
        url = f"{self.base_url}{U64_API.RUN_PRG}"
        try:
            r = self.session.post(
                url,
                files={"file": ("c64cast.prg", BASIC_CLEAR_LOOP_PRG)},
                timeout=timeout,
            )
            r.raise_for_status()
        except requests.RequestException as e:
            log.warning("run_prg (clear loop) failed: %s", e)

    def launch_program(self, path: str, timeout: float = 10.0) -> None:
        """Upload and run a C64 program on the real machine.

        Picks the firmware runner by file extension: `.prg` → run_prg
        (loads + RUNs the program), `.crt` → run_crt (resets with the
        cartridge active). The program then owns the machine — c64cast
        stops painting and `LauncherScene` only polls for player input.

        Unlike `run_basic_clear_loop`, failures re-raise: the caller
        (LauncherScene.setup) needs to know the launch never happened so
        it can advance instead of idling on a black screen. The multipart
        field name (`file`) and `.crt` endpoint shape mirror run_prg; if a
        future firmware names the cart attachment differently this is the
        one spot to adjust.
        """
        ext = os.path.splitext(path)[1].lower()
        if ext == ".crt":
            endpoint = U64_API.RUN_CRT
        elif ext == ".prg":
            endpoint = U64_API.RUN_PRG
        else:
            raise ValueError(
                f"launch_program: unsupported extension {ext!r} for {path!r} "
                f"(expected .prg or .crt)"
            )

        with open(path, "rb") as fh:
            payload = fh.read()

        self.flush()
        self.invalidate_cache()
        url = f"{self.base_url}{endpoint}"
        name = os.path.basename(path)
        try:
            r = self.session.post(
                url,
                files={"file": (name, payload)},
                timeout=timeout,
            )
            r.raise_for_status()
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                raise RuntimeError(
                    f"U64 endpoint {url} returned 404 — the {ext} runner "
                    "is required to launch this program (check firmware "
                    "version)."
                ) from e
            raise

    def reset(self) -> None:
        """Hard machine reset. Invalidates the delta cache since the C64 side
        will be reinitialized.

        Blanks the VIC display first (best-effort) so a hires / bitmap scene
        doesn't flash its leftover RAM as a glitchy image during the
        reset-latency window — the VIC holds the outgoing mode + bank until
        the kernal reinitializes it, so without this a bitmap scene shows
        garbage for a few hundred ms before the boot screen. Guarded + flushed
        so the blank lands before the reset takes effect; a dead socket on
        shutdown just skips it (the reset PUT still fires).

        No pre-flush of the general write stream: reset wipes the state any
        OTHER in-flight writes would touch, so waiting for them is pointless
        and adds a stall on shutdown if the socket has gone unresponsive."""
        try:
            self.blank_display()
            self.flush()
        except Exception as e:
            log.debug("U64 reset: pre-reset display blank skipped (%s)", e)
        self.invalidate_cache()
        try:
            self.session.put(self.reset_url, timeout=2.0)
        except requests.RequestException as e:
            log.warning("U64 reset failed: %s", e)

    def _launch_sid_player(
        self,
        parsed: ParsedPsid,
        layout: _PlayerLayout,
        mc: bytes,
        reinit: bytes,
        timeout: float,
        avoid: bytes | bytearray | None = None,
        defer_audio: bool = False,
    ) -> bool:
        """Ultimate kick: DMA the SID payload + player MC + re-INIT stub, flush
        so all three land, then POST a `10 SYS <player_base>` PRG to the REST
        run_prg runner. run_prg soft-resets the C64 (RAM preserved — the player
        MC at $C300 survives) and RUNs the stub; BASIC's SYS jumps to the player
        MC, which installs the IRQ and spins forever (never re-entering BASIC).

        `avoid` is unused here (no trampoline to place). `defer_audio` is ignored:
        run_prg is a synchronous reset+RUN that also re-inits VIC to text mode, so
        there's no loaded-but-silent window to hold — audio starts here. Returns
        True so `run_sid_player` runs the standard finalize (timestamp + divider).
        WaveformScene's `begin_sid_audio()` is then a no-op, and it (re)asserts the
        bitmap display *after* this call as it always has."""
        self._write_sid_blobs(parsed, layout, mc, reinit)
        self.flush()
        basic_stub = _build_basic_sys_stub(layout.player_base)
        url = f"{self.base_url}{U64_API.RUN_PRG}"
        try:
            r = self.session.post(
                url,
                files={"file": ("sidplayer.prg", basic_stub)},
                timeout=timeout,
            )
            r.raise_for_status()
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                raise RuntimeError(
                    f"U64 endpoint {url} returned 404 — run_prg is "
                    "required for the SID player path."
                ) from e
            raise
        return True

    # ---- lifecycle / introspection ----------------------------------------
    def probe(self, timeout: float = 2.0) -> str | None:
        """Verify the U64 REST endpoint is reachable. Returns a status string
        on success, or None on failure. Use to fail fast at startup with a
        clear message. (DMA connectivity is verified separately by the
        SocketDMAClient.connect() in __init__.)"""
        try:
            r = self.session.get(self.base_url + "/", timeout=timeout)
            return f"HTTP {r.status_code}"
        except requests.RequestException as e:
            log.debug("probe failed: %s", e)
            return None

    def flush(self) -> None:
        """Block until every queued DMA write has been processed by the U64.

        Implementation: trailing IDENTIFY round-trip on the DMA socket; by
        the per-connection FIFO guarantee, the reply arrives only after
        every prior DMAWRITE has executed. Call before any REST runner
        (reset / run_sid_player / run_basic_clear_loop) so the runner doesn't
        race ahead of in-flight scene writes."""
        try:
            self.socket_dma.flush()
        except (OSError, SocketDMAError) as e:
            log.warning("dma flush failed: %s", e)

    def reu_write(self, reu_offset: int, data: bytes) -> None:
        """Bus-clean write into FPGA-mapped REU SRAM at 24-bit ``reu_offset``.

        Forwards to the socket DMA client's REUWRITE opcode. Part of the
        capability-gated `C64Backend` surface (``profile.supports_reu``);
        existing audio/video REU paths still reach `self.socket_dma.reuwrite`
        directly, this is the backend-agnostic entry point."""
        self.socket_dma.reuwrite(reu_offset, data)

    def close(self) -> None:
        self.socket_dma.close()
        self.session.close()

    def format_write_latency(self) -> str | None:
        """One-line summary of per-DMA-write latency suitable for the log.
        Returns None when no samples have been recorded yet."""
        return self.socket_dma.format_latency()
