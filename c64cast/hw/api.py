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
from dataclasses import dataclass, replace
from typing import NamedTuple
from urllib.parse import urlparse

import requests

from .backend import (
    EMUSID_MIXER_CATEGORY,
    SID_CONFIG_CATEGORIES,
    SYSTEM_MODE_CATEGORY,
    ULTIMATE_PROFILE,
    BackendCapabilityError,
    BufferedWriteBackend,
    HardwareProfile,
)
from .c64 import (
    CIA1,
    CPU,
    KERNAL,
    ROM,
    U64_API,
    VECTORS,
    actual_rate_for_latch,
    cia1_latch_for_rate,
    frame_rate,
    kernal_cia1_latch,
)
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

# C64-side SID player. Default base $C300 (just past audio_handlers.py's
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
# [_SidPlayerMixin._tune_play_divider] after INIT settles. Default N=1
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
#    [_SidPlayerMixin._tune_play_divider] after INIT.
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

# Audio handler region — audio.AudioStreamer installs the NMI DAC at $C020
# and REU pump handlers at $C100-$C2FF (handler bytes in audio_handlers.py). Refuse player layouts that would overlap so
# we don't clobber bytes the audio path may read/write under us.
_AUDIO_REGION_LO = 0xC000
_AUDIO_REGION_HI = 0xC300  # exclusive

# Highest legal end address for the player bundle. $D000+ is I/O space.
_PLAYER_BUNDLE_HI_MAX = 0xD000

# The bus window the U2+'s emulated stereo SIDs snoop. Its firmware takes SID
# writes off the cartridge port, which carries no signal distinguishing an I/O
# access from one to the RAM underneath — so a tune whose payload or buffers
# live in RAM here is heard as register writes by the emulations. Harmless on
# the C64's own audio output (where the real chips decode properly), audible
# garbage on the Ultimate's. Warned about, never refused: the tune plays
# correctly and only one of the two outputs is affected.
_EMUSID_SNOOP_LO = 0xD400
_EMUSID_SNOOP_HI = 0xD800  # exclusive

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
    free RAM (above $0820, below $D000), don't overlap audio_handlers.py's
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
    free of `avoid`, the SID payload, and audio_handlers.py's $C000-$C2FF region,
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


# PSID v2+ flags ($76-$77, big-endian): clock is bits 2-3 of the LOW byte.
# Same table as sid_host_emu._CLOCK_TABLE, duplicated because hw must not
# import sid; tests/test_api.py asserts they agree.
_PSID_CLOCK_TABLE = {0: "?", 1: "PAL", 2: "NTSC", 3: "PAL+NTSC"}


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
    # PSID v2+ clock flag: "PAL", "NTSC", "PAL+NTSC", "?" or None (v1 header).
    # Decoded here as well as in sid_host_emu.parse_sid_header because `hw`
    # must not import `sid`; tests/test_api.py pins the two against each other.
    clock: str | None = None
    # PSID `speed` word ($12-$15, big-endian): one bit per subtune, 0 = the
    # tune expects PLAY once per video frame ("vsync"), 1 = its INIT programs
    # CIA #1 Timer A and self-times. Songs past 32 reuse bit 31.
    speed: int = 0

    def song_is_vsync(self, song: int | None = None) -> bool:
        """True when subtune `song` (1-based; default `song_to_play`) expects
        PLAY once per video frame rather than off its own CIA timer."""
        bit = min((song if song is not None else self.song_to_play) - 1, 31)
        return not (self.speed >> bit) & 1


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
    version = int.from_bytes(sid_bytes[4:6], "big")
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
    clock = None
    if version >= 2 and len(sid_bytes) >= 0x78:
        clock = _PSID_CLOCK_TABLE[(sid_bytes[0x77] >> 2) & 0x03]
    return ParsedPsid(
        load_addr=load_addr,
        init_addr=init_addr,
        play_addr=play_addr,
        num_songs=num_songs,
        start_song=start_song,
        song_to_play=song_to_play,
        payload=payload,
        clock=clock,
        speed=int.from_bytes(sid_bytes[0x12:0x16], "big"),
    )


# ---------------------------------------------------------------------------
# SID player — host-side orchestration
# ---------------------------------------------------------------------------
class _SidLaunch(NamedTuple):
    """One SID-player launch, bundled for the backend-specific kick: the
    parsed tune, the resolved layout, the built player MC + re-INIT stub
    blobs, and the caller's launch options."""

    parsed: ParsedPsid
    layout: _PlayerLayout
    mc: bytes
    reinit: bytes
    timeout: float
    avoid: bytes | bytearray | None = None
    defer_audio: bool = False


class _SidPlayerMixin(BufferedWriteBackend):
    """SID-player state + the host-side work of running a tune on the 6510.

    All of it is backend-agnostic — PSID parse, player-layout choice, player MC
    / re-INIT stub build, the CIA #1 PLAY-rate divider auto-tune, SHIFT-driven
    subtune re-INIT (`cue_song_reinit`), and the audio-start instant the scope's
    host-emu clock anchors to — and rides only the buffered write path +
    `read_memory`. The one backend-specific step is `_launch_sid_player`, the
    kick that hands the CPU to the uploaded player MC:

      * Ultimate — POST a `10 SYS <player_base>` PRG to the REST `run_prg`
        runner (a soft reset that preserves RAM, then RUN), which starts audio
        synchronously.
      * TeensyROM — swap `$0314` to the re-INIT stub so the next kernal IRQ
        runs INIT and installs the PLAY handler; no reset, no boot, and the
        start is deferrable (see teensyrom_api.TeensyROMBackend).

    Mixed into each concrete backend alongside `_StubRunnerBackend`, whose stub
    running it shares the *shape* of but no state. It overrides the ABC's
    capability-gated (raising) `run_sid_player` / `cue_song_reinit` with the
    working implementations, so a backend that mixes it in also sets
    `profile.supports_run_prg = True`.
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
        # The tune currently loaded + the [ultimate64].sid_play_rate setting it
        # was launched with. Kept because the PSID speed flag is PER SUBTUNE:
        # cue_song_reinit has to re-decide whether the new song is vsync-timed.
        self._sid_parsed: ParsedPsid | None = None
        self._sid_play_rate: str | float | None = None
        # True once _apply_play_rate has actually written a latch, so teardown
        # only writes the kernal default back over a latch we put there.
        self._sid_play_rate_applied = False
        # What a vsync tune's PLAY is really being called at right now — the
        # retuned rate, or the KERNAL's jiffy rate when we left it alone. The
        # scope's host emulator ticks at this so it stays locked to the audio.
        self._sid_vsync_play_rate_hz: float | None = None
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
    def _launch_sid_player(self, launch: _SidLaunch) -> bool:
        """DMA the SID payload + player MC + re-INIT stub into C64 RAM and hand
        control to the player MC. Use `_write_sid_blobs` for the standard
        three-blob upload. `launch.avoid` is the caller's RAM footprint bitmap
        (or None), forwarded for backends that need it.

        Returns True to have `run_sid_player` run the standard post-start
        finalize — record the audio-start instant + auto-tune the PLAY-rate
        divider — used by a backend that starts audio synchronously right here
        (the Ultimate's `run_prg`). Returns False if the backend manages that
        itself: either the start is deferred to `begin_sid_audio()`, or the
        backend self-finalizes after its own kick (the TeensyROM, whose `$0314`
        vector-swap must precede the divider's CIA #1 read, so it can't let
        `run_sid_player` finalize before the swap)."""
        ...

    def _write_sid_blobs(self, launch: _SidLaunch) -> None:
        """DMA the SID payload + patched player MC + re-INIT stub to their C64
        addresses. Invalidates the delta cache first (the payload + player MC
        overlap arbitrary RAM regions; a clean baseline keeps the next scene's
        writes diffing against fresh state). Does NOT flush — the caller flushes
        once all blobs (plus any backend-specific extras, e.g. the TR
        trampoline) have been queued, so they all land before the BASIC SYS
        fires."""
        self.invalidate_cache()
        self.write_memory_file(f"{launch.parsed.load_addr:04X}", launch.parsed.payload)
        self.write_memory_file(f"{launch.layout.player_base:04X}", launch.mc)
        self.write_memory_file(f"{launch.layout.stub_base:04X}", launch.reinit)

    def _warn_if_payload_snooped(self, parsed: ParsedPsid) -> None:
        """Warn when a tune's payload lands in the RAM under ``$D400-$D7FF`` on
        a backend whose emulated SIDs snoop that window (the U2+). Only the
        Ultimate's own audio output is affected — the C64's is fed by real
        chips, which decode I/O properly — so this is a warning about one
        listening path, not a reason to refuse a tune that plays fine.

        Catches the load-time case only. A tune that merely *uses* that RAM at
        run time hits the same problem and can't be detected from the header;
        the message says so rather than implying the check is exhaustive."""
        if not self.profile.supports_emusid_mixer:
            return
        payload_hi = parsed.load_addr + len(parsed.payload)
        if parsed.load_addr >= _EMUSID_SNOOP_HI or payload_hi <= _EMUSID_SNOOP_LO:
            return
        log.warning(
            "SID payload $%04X-$%04X overlaps $%04X-$%04X, the window this "
            "device's emulated SIDs snoop off the cartridge port — which can't "
            "tell those writes from real SID writes. Expect clicks or stray "
            "notes on the Ultimate's audio output (the C64's own output is "
            "unaffected). Tunes that use this RAM only at run time have the "
            "same effect and can't be detected here.",
            parsed.load_addr,
            payload_hi,
            _EMUSID_SNOOP_LO,
            _EMUSID_SNOOP_HI - 1,
        )

    def run_sid_player(
        self,
        sid_bytes: bytes,
        song: int = 0,
        timeout: float = 5.0,
        *,
        avoid: bytes | bytearray | None = None,
        play_bank: int | None = None,
        defer_audio: bool = False,
        play_rate: str | float | None = None,
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
          6. Measure the post-INIT CIA #1 Timer A rate, optionally retune it to
             the tune's own PLAY rate (`play_rate`), and patch the player MC's
             kernal-chain divider so fast-PLAY tunes don't run SCNKEY every tick.

        `song` is the 1-based subtune; pass 0 to use the SID's default.

        `play_rate` sets what a *vsync-timed* tune's PLAY is called at, by
        reprogramming CIA #1 Timer A after INIT (see `_apply_play_rate` for why
        after, and for the gates):

          * None / "off" — leave the KERNAL's jiffy latch alone. That is ~60 Hz
            on BOTH standards, so a PAL tune plays ~19.7% fast. This was the
            only behaviour before the option existed.
          * "auto" — the frame rate of the tune's own PSID clock flag, so a PAL
            tune plays at ~50.12 Hz on either machine.
          * a float — that rate in Hz, for every vsync tune regardless of flag.

        CIA-timed (multispeed) tunes are never touched: their INIT programs
        Timer A itself and is the authority on their tempo.

        `defer_audio=True` loads the player but leaves it silent until
        `begin_sid_audio()` — WaveformScene uses it to bring the oscilloscope up
        before the first note. A backend that can't defer (the Ultimate's
        synchronous `run_prg`) starts immediately and ignores the flag.

        v1 limitations: PSID only. See [docs/caveats.md] for the full rationale.
        """
        parsed = parse_psid_for_player(sid_bytes, song=song)
        self._sid_parsed = parsed
        self._sid_play_rate = play_rate
        self._warn_if_payload_snooped(parsed)
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
        launch = _SidLaunch(
            parsed, layout, mc, reinit, timeout=timeout, avoid=avoid, defer_audio=defer_audio
        )
        finalize = self._launch_sid_player(launch)

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
        # The PSID speed flag is per-subtune, so the play-rate decision is
        # re-made here for `song` rather than inherited from the start song.
        self._tune_play_divider(settle_s=0.08, song=song)

    # CIA #1 Timer A latch sampling for [_tune_play_divider]. $DC04/$DC05 are
    # write-only; a read returns the live down-count, so the latch is estimated
    # as the max over a burst of reads.
    #
    # That max is biased LOW, and by more than it looks: the count is roughly
    # uniform over [0, latch], so the max of n reads averages n/(n+1) of the
    # true latch and the tail is fat — at n=8, one run in six lands below 0.8
    # and one in sixty below 0.6. Measured on hardware, an 8-sample burst
    # against a kernal 60 Hz jiffy reported 75.6 Hz.
    #
    # 16 keeps that tail off `_apply_play_rate`'s self-timed floor (a false
    # trip there silently skips the tempo correction) and costs ~110 ms of
    # REST reads once per tune, inside a settle window that already exists.
    _DIVIDER_LATCH_SAMPLES = 16

    # Target kernal-services rate. SCNKEY at >= 30 Hz keeps $028D updating fast
    # enough for the 10 Hz keyboard poller.
    _DIVIDER_TARGET_KERNAL_HZ = 30

    # Cap the divider so a misread (very high estimated PLAY rate) can't starve
    # kernal services entirely.
    _DIVIDER_MAX = 8

    # PHI2 approximation in Hz. PAL is 985248, NTSC is 1022730 — using 1e6
    # introduces <2% error, well within the rounding tolerance.
    _DIVIDER_PHI2_HZ = 1_000_000

    # Floor, as a fraction of this machine's kernal latch, below which the
    # sampled post-INIT latch is taken as proof the tune reprogrammed Timer A
    # regardless of what its speed flag claims — see `_apply_play_rate`.
    _PLAY_RATE_SELF_TIMED_BELOW = 0.6

    def target_play_rate_hz(self, song: int | None = None) -> float | None:
        """The rate PLAY should be called at for the loaded tune's subtune
        `song`, or None to leave the KERNAL's jiffy latch alone.

        None whenever: no tune is loaded, the setting is off, the subtune is
        CIA-timed (it self-times — its INIT is the authority), or the setting
        is "auto" and the tune declares no single definite standard (a v1
        header, "PAL+NTSC", or "?" — there is nothing to infer from)."""
        parsed, setting = self._sid_parsed, self._sid_play_rate
        if parsed is None or setting is None or setting == "off":
            return None
        if not parsed.song_is_vsync(song):
            return None
        if isinstance(setting, str):
            return frame_rate(parsed.clock) if parsed.clock in ("PAL", "NTSC") else None
        return float(setting)

    def _apply_play_rate(self, sampled_latch: int, song: int | None = None) -> float | None:
        """Reprogram CIA #1 Timer A so a vsync-timed tune's PLAY runs at its
        own frame rate. Returns the resulting rate in Hz, or None if nothing
        was written.

        WHY THIS RUNS AFTER INIT rather than before: on the Ultimate the player
        is kicked via `run_prg`, which soft-resets the C64 — and the KERNAL's
        reset path reloads Timer A. Any latch written before the kick is gone
        before the first PLAY. So the write lands here, ~200 ms in, and the
        gates below are what keep it off tunes that time themselves.

        Two gates, because the PSID speed flag alone is not trustworthy enough
        to overwrite a tune's own timer with:

          * the flag must say vsync (`target_play_rate_hz`), and
          * the latch sampled after INIT must not already be far below this
            machine's kernal default. A tune running 2x/3x/4x multispeed sits
            at 1/2, 1/3, 1/4 of it — well clear of the noise in an 8-sample
            max of a free-running down-counter, which is what `sampled_latch`
            is.

        Best-effort: a failed write logs and returns None, leaving the tune at
        the kernal rate exactly as before."""
        rate = self.target_play_rate_hz(song)
        if rate is None:
            return None
        system = self.profile.system
        floor = kernal_cia1_latch(system) * self._PLAY_RATE_SELF_TIMED_BELOW
        if sampled_latch < floor:
            log.info(
                "SID player: tune is flagged vsync but INIT left CIA1 at ~$%04X "
                "(well under this machine's kernal $%04X) — treating it as "
                "self-timed and leaving its rate alone",
                sampled_latch,
                kernal_cia1_latch(system),
            )
            return None
        latch = cia1_latch_for_rate(rate, system)
        try:
            self.write_memory(
                f"{CIA1.TIMER_A_LO:04X}", f"{latch & 0xFF:02X}{(latch >> 8) & 0xFF:02X}"
            )
            self.flush()
        except Exception:
            log.warning("SID player: could not set the PLAY rate to %.3f Hz", rate, exc_info=True)
            return None
        self._sid_play_rate_applied = True
        return actual_rate_for_latch(latch, system)

    def sid_vsync_play_rate_hz(self) -> float:
        """The rate a vsync-timed tune's PLAY is actually being called at on
        this machine right now — the retuned rate when `_apply_play_rate`
        wrote one, else the KERNAL's own jiffy rate.

        Note the fallback is ~60 Hz on BOTH standards: the jiffy IRQ is a
        wall-clock service, not a frame interrupt. Anything modelling the
        tune's progress (the scope's host emulator) has to tick at this, not at
        the video frame rate."""
        if self._sid_vsync_play_rate_hz is not None:
            return self._sid_vsync_play_rate_hz
        system = self.profile.system
        return actual_rate_for_latch(kernal_cia1_latch(system), system)

    def restore_kernal_play_rate(self) -> None:
        """Put CIA #1 Timer A back to this machine's kernal default, undoing
        `_apply_play_rate`. Called at SID-scene teardown so the jiffy clock,
        SCNKEY and the cursor blink resume at ~60 Hz. No-op when the rate was
        never overridden. Best-effort — teardown must not raise."""
        self._sid_vsync_play_rate_hz = None
        if not self._sid_play_rate_applied:
            return
        self._sid_play_rate_applied = False
        latch = kernal_cia1_latch(self.profile.system)
        try:
            self.write_memory(
                f"{CIA1.TIMER_A_LO:04X}", f"{latch & 0xFF:02X}{(latch >> 8) & 0xFF:02X}"
            )
        except Exception as e:
            log.debug("SID player: kernal PLAY-rate restore failed: %s", e)

    def _tune_play_divider(self, settle_s: float = 0.2, song: int | None = None) -> int:
        """Sample CIA #1 Timer A to estimate the SID's PLAY rate, retune it to
        the tune's own rate when `[ultimate64].sid_play_rate` asks for that
        (`_apply_play_rate`), then live-patch the player MC's tick divider so
        the kernal IRQ tail (SCNKEY + UDTIM + cursor blink at $EA31) only runs
        every Nth PLAY tick.

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
        retuned = self._apply_play_rate(max_count, song)
        if retuned is not None:
            log.info(
                "SID player: vsync %s tune — PLAY retuned %.2fHz -> %.2fHz (CIA1 latch $%04X)",
                (self._sid_parsed.clock if self._sid_parsed else None) or "?",
                play_rate_hz,
                retuned,
                cia1_latch_for_rate(retuned, self.profile.system),
            )
            play_rate_hz = retuned
        # Record what a vsync tune's PLAY now runs at, for the scope's clock.
        # A CIA-timed tune's own rate isn't this and isn't wanted here — the
        # host emulator derives that from the latch its INIT wrote.
        if self._sid_parsed is not None and self._sid_parsed.song_is_vsync(song):
            self._sid_vsync_play_rate_hz = play_rate_hz
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


# ---------------------------------------------------------------------------
# Character-ROM dump
# ---------------------------------------------------------------------------
#
# The character ROM is not RAM: a host `read_memory($D000)` sees the I/O page
# (VIC/SID/CIA registers), because what `$01` maps is decided on the C64, at
# read time. So the only way to get the charset off a machine is to run code
# ON it — bank CHAREN out, copy the ROM down into plain RAM, restore the bank,
# and let the host read the copy back. That is this stub. Consumers:
# [c64cast.hw.char_rom], `--dump-char-rom`, and the first-run auto-dump.
#
# PLACEMENT — both addresses are load-bearing:
#
#  * The landing zone must be RAM that is readable UNDER DEFAULT BANKING,
#    since `read_memory` sees whatever `$01` is at read time (i.e. $37). That
#    rules out the $A000/$D000 underlay RAM, which needs a non-default bank to
#    reach. $C000-$CFFF is the proven-safe high RAM the SID player already
#    lives in — no ROM over it, and it survives `run_prg`'s soft reset
#    (RAMTAS's memory-size scan restores every byte it probes).
#  * The stub cannot share those 4 KB — the copy would overwrite it mid-flight
#    — and cannot live at $0200-$03FF, because RAMTAS zeroes the cassette
#    buffer on every reset and the Ultimate kick *is* a reset. $8100 is BASIC
#    program RAM: far above the one-line `SYS` program run_prg loads at $0801
#    (which creates no variables, so BASIC never grows into it), and clear of
#    the $8004 cartridge-signature window the KERNAL checks at reset.
#
# COMPLETION FLAG: the last byte of the blob is uploaded as $00 and set to $FF
# by the stub as its final act. Polling that one byte is how the host knows the
# ~45 ms copy has finished — the alternative (sleep and hope) has no signal at
# all on the Ultimate, whose `run_prg` POST returns once BASIC has *started*
# the program, not once `SYS` has returned.
CHAR_ROM_DUMP_STUB_ADDR = 0x8100
CHAR_ROM_DUMP_DEST = 0xC000
CHAR_ROM_SRC_PAGE = 0xD0
CHAR_ROM_DUMP_PAGES = 16
CHAR_ROM_DUMP_BYTES = CHAR_ROM_DUMP_PAGES * 256  # the full 4 KB CHARGEN

# Patch offsets into the template below.
_CR_PATCH_SRC_HI = 14  # LDA $D000,Y operand high (self-modified per page)
_CR_PATCH_DST_HI = 17  # STA $C000,Y operand high (self-modified per page)
_CR_PATCH_INC_SRC_LO = 22  # INC <src_hi operand> address low
_CR_PATCH_INC_SRC_HI = 23
_CR_PATCH_INC_DST_LO = 25  # INC <dst_hi operand> address low
_CR_PATCH_INC_DST_HI = 26
_CR_PATCH_PAGES = 29  # CPX #<pages>
_CR_PATCH_FLAG_LO = 38  # STA <flag> address low
_CR_PATCH_FLAG_HI = 39

# Common body: mask IRQs, save + switch the bank, copy 16 pages with
# self-modified page pointers (X/Y only — no zero page, so nothing of BASIC's
# is clobbered), restore the bank, raise the completion flag.
_CHAR_ROM_DUMP_BODY = bytes(
    [
        0x78,  # 00  SEI        (the KERNAL IRQ cannot ack CIA #1 with
        #                        I/O banked out — it would re-fire forever)
        0xA5,
        CPU.PORT,  # 01  LDA $01
        0x48,  # 03  PHA        (save the caller's bank)
        0xA9,
        CPU.PORT_CHARROM,  # 04  LDA #$33   (CHAREN=0 → character ROM
        #                             at $D000-$DFFF)
        0x85,
        CPU.PORT,  # 06  STA $01
        0xA2,
        0x00,  # 08  LDX #$00   (page counter)
        # --- page loop @ 10 ------------------------------------------------
        0xA0,
        0x00,  # 10  LDY #$00
        # --- byte loop @ 12 ------------------------------------------------
        0xB9,
        0x00,
        0xD0,  # 12  LDA $D000,Y  (high byte patched + self-modified)
        0x99,
        0x00,
        0xC0,  # 15  STA $C000,Y  (high byte patched + self-modified)
        0xC8,  # 18  INY
        0xD0,
        0xF7,  # 19  BNE -9 → 12
        0xEE,
        0x00,
        0x00,  # 21  INC <src page byte>   (patched)
        0xEE,
        0x00,
        0x00,  # 24  INC <dst page byte>   (patched)
        0xE8,  # 27  INX
        0xE0,
        0x10,  # 28  CPX #<pages>          (patched)
        0xD0,
        0xEA,  # 30  BNE -22 → 10
        0x68,  # 32  PLA
        0x85,
        CPU.PORT,  # 33  STA $01    (restore the caller's bank)
        0xA9,
        0xFF,  # 35  LDA #$FF
        0x8D,
        0x00,
        0x00,  # 37  STA <flag>            (patched; the host polls this)
    ]
)

# Tail for the Ultimate kick: BASIC `SYS` called us, so re-enable IRQs and
# return to it.
_CHAR_ROM_DUMP_TAIL_SYS = bytes(
    [
        0x58,  # CLI
        0x60,  # RTS
    ]
)

# Tail for the TeensyROM kick: we ARE the kernal IRQ handler (reached through a
# $0314 vector swap), so restore the vector — one run only — and exit through
# the kernal tail, which acks CIA #1 and RTIs. No CLI: RTI restores the I flag
# from the stacked status byte. Same shape as the SID re-INIT stub.
_CHAR_ROM_DUMP_TAIL_IRQ = bytes(
    [
        0xA9,
        KERNAL.IRQ_HANDLER & 0xFF,  # LDA #$31
        0x8D,
        VECTORS.IRQ & 0xFF,
        (VECTORS.IRQ >> 8) & 0xFF,  # STA $0314
        0xA9,
        (KERNAL.IRQ_HANDLER >> 8) & 0xFF,  # LDA #$EA
        0x8D,
        (VECTORS.IRQ + 1) & 0xFF,
        ((VECTORS.IRQ + 1) >> 8) & 0xFF,  # STA $0315
        0x4C,
        KERNAL.IRQ_HANDLER & 0xFF,
        (KERNAL.IRQ_HANDLER >> 8) & 0xFF,  # JMP $EA31
    ]
)


def build_char_rom_dump_stub(*, base: int = CHAR_ROM_DUMP_STUB_ADDR, irq_exit: bool) -> bytes:
    """The character-ROM dump stub, assembled for `base`.

    `irq_exit` picks the tail: False returns via `RTS` (entered from a BASIC
    `SYS`, the Ultimate kick), True restores `$0314` and chains to `$EA31`
    (entered as the kernal IRQ handler, the TeensyROM kick).

    The returned blob is DMA'd verbatim to `base`; its **last byte is the
    completion flag**, uploaded as $00 and set to $FF by the stub, so the flag
    address is always `base + len(stub) - 1` (:func:`char_rom_flag_addr`).
    """
    stub = bytearray(_CHAR_ROM_DUMP_BODY)
    stub[_CR_PATCH_SRC_HI] = CHAR_ROM_SRC_PAGE
    stub[_CR_PATCH_DST_HI] = (CHAR_ROM_DUMP_DEST >> 8) & 0xFF
    _patch_word(stub, _CR_PATCH_INC_SRC_LO, _CR_PATCH_INC_SRC_HI, base + _CR_PATCH_SRC_HI)
    _patch_word(stub, _CR_PATCH_INC_DST_LO, _CR_PATCH_INC_DST_HI, base + _CR_PATCH_DST_HI)
    stub[_CR_PATCH_PAGES] = CHAR_ROM_DUMP_PAGES
    stub += _CHAR_ROM_DUMP_TAIL_IRQ if irq_exit else _CHAR_ROM_DUMP_TAIL_SYS
    stub += b"\x00"  # completion flag, raised by the stub
    _patch_word(stub, _CR_PATCH_FLAG_LO, _CR_PATCH_FLAG_HI, base + len(stub) - 1)
    return bytes(stub)


def char_rom_flag_addr(stub: bytes, base: int = CHAR_ROM_DUMP_STUB_ADDR) -> int:
    """Address of `stub`'s completion flag byte — always its last byte."""
    return base + len(stub) - 1


# How long to wait for the stub's completion flag. The copy itself is ~45 ms
# (4096 bytes × ~11 cycles at 1 MHz); the rest of the budget covers the
# Ultimate's reset + BASIC bring-up, which `run_prg` may return ahead of.
_CHAR_ROM_FLAG_TIMEOUT_S = 6.0
_CHAR_ROM_FLAG_POLL_S = 0.05


class _StubRunnerBackend(BufferedWriteBackend):
    """On-C64 stub orchestration shared by the Ultimate + TeensyROM backends.

    A feature that needs code running on the real 6510 splits the same way:
    identical host-side work — which touches only the buffered write path +
    `read_memory` — and one backend-specific step that hands the CPU to the
    uploaded stub. This class is the **character-ROM dump**: `dump_char_rom`
    uploads the copy stub, waits on its completion flag and reads the landing
    zone back; the backend-specific step is `_kick_char_rom_dump` (run_prg vs.
    a `$0314` vector swap). The SID player splits the same way in
    `_SidPlayerMixin`, which each concrete backend inherits alongside this one.

    Both real backends set `profile.supports_run_prg = True`; this class
    overrides the ABC's capability-gated (raising) `dump_char_rom` with the
    working implementation.
    """

    # ---- backend-specific kick (subclass implements) ----------------------
    @abstractmethod
    def _kick_char_rom_dump(self, stub_addr: int, timeout: float) -> None:
        """Hand the CPU to the already-uploaded character-ROM dump stub at
        `stub_addr`. Returning does NOT mean the copy finished — `dump_char_rom`
        polls the stub's completion flag for that.

        The two kicks differ in whether the stub is entered as ordinary code or
        as an interrupt handler, which is why `build_char_rom_dump_stub` takes
        `irq_exit`: the Ultimate SYSes it from BASIC (`RTS` tail), the
        TeensyROM swaps `$0314` to it (`JMP $EA31` tail)."""
        ...

    def _char_rom_stub_wants_irq_exit(self) -> bool:
        """True when `_kick_char_rom_dump` enters the stub as the kernal IRQ
        handler (the TeensyROM's `$0314` swap) rather than as a BASIC `SYS`."""
        return False

    def dump_char_rom(self, timeout: float = 10.0) -> bytes:
        """Read the C64's character ROM by running a copy stub on the machine.

        Uploads the stub (see `build_char_rom_dump_stub`), kicks it via the
        backend hook, waits for its completion flag, then reads the 4 KB landing
        zone at `$C000` back over the ordinary read path. The flag poll is what
        makes this deterministic: the copy takes ~45 ms of 6510 time that
        neither kick's return tells us about.

        Raises RuntimeError if the stub never signaled or the read-back failed,
        and BackendCapabilityError if this backend can't read C64 memory.
        Verifying that the bytes *are* a charset is `char_rom.dump`'s job.
        """
        if not self.profile.supports_read:
            raise BackendCapabilityError("dump_char_rom (needs read_memory)")
        irq_exit = self._char_rom_stub_wants_irq_exit()
        stub = build_char_rom_dump_stub(base=CHAR_ROM_DUMP_STUB_ADDR, irq_exit=irq_exit)
        flag_addr = char_rom_flag_addr(stub, CHAR_ROM_DUMP_STUB_ADDR)

        # The landing zone overlaps regions other writers own ($C000-$C2FF is
        # audio's NMI/REU handler area, $C300+ the SID player), so drop the
        # delta cache: the next scene must diff against fresh state.
        self.invalidate_cache()
        self.write_memory_file(f"{CHAR_ROM_DUMP_STUB_ADDR:04X}", stub)
        self.flush()
        self._kick_char_rom_dump(CHAR_ROM_DUMP_STUB_ADDR, timeout)

        deadline = time.time() + _CHAR_ROM_FLAG_TIMEOUT_S
        while time.time() < deadline:
            flag = self.read_memory(flag_addr, 1)
            if flag == b"\xff":
                break
            time.sleep(_CHAR_ROM_FLAG_POLL_S)
        else:
            raise RuntimeError(
                f"the character-ROM dump stub never signaled completion "
                f"(flag at ${flag_addr:04X} still clear after "
                f"{_CHAR_ROM_FLAG_TIMEOUT_S:.0f}s) — it may not have been reached"
            )

        data = self.read_memory(CHAR_ROM_DUMP_DEST, CHAR_ROM_DUMP_BYTES, timeout=timeout)
        if data is None or len(data) != CHAR_ROM_DUMP_BYTES:
            got = "nothing" if data is None else f"{len(data)} bytes"
            raise RuntimeError(
                f"could not read the dumped character ROM back from "
                f"${CHAR_ROM_DUMP_DEST:04X} (got {got}, want {CHAR_ROM_DUMP_BYTES})"
            )
        return data


class Ultimate64API(_SidPlayerMixin, _StubRunnerBackend):
    def __init__(
        self,
        base_url: str,
        *,
        dma_port: int = DEFAULT_PORT,
        dma_password: str | None = None,
        profile: HardwareProfile | None = None,
    ):
        # Init the SID-player state (_SidPlayerMixin) and, below it in the MRO,
        # the shared write path (delta cache, stats, listeners).
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

    def put_config_item(
        self, category: str, item: str, value: str, *, timeout: float = 3.0
    ) -> None:
        """Set one Ultimate config item LIVE over the REST config API.

        ``PUT /v1/configs/<category>/<item>?value=<value>``. The firmware
        applies the change immediately through its per-item effectuate hook —
        no reboot (verified in the 1541ultimate source: C64::effectuate_settings
        / U64Config::setCpuSpeed) — and does NOT persist it to flash, so it
        reverts on the next power-cycle (we never call ``:save_to_flash``).
        Used by the REU auto-provisioner (hw_provision.provision_reu) to enable + size
        the REU for a run and to restore the original at teardown. Raises
        ``requests.RequestException`` on transport/HTTP failure; callers treat
        provisioning as best-effort (a config we can't write just leaves the
        existing doctor/probe degradation in place)."""
        from urllib.parse import quote

        url = f"{self.base_url}/v1/configs/{quote(category)}/{quote(item)}"
        r = self.session.put(url, params={"value": value}, timeout=timeout)
        r.raise_for_status()

    def get_config_category(self, category: str, *, timeout: float = 3.0) -> dict[str, str]:
        """Read one Ultimate config category LIVE over the REST config API.

        ``GET /v1/configs/<category>`` → ``{"<category>": {"<item>": <value>,
        ...}}`` (see the firmware's ``emit_store``). Returns the inner
        ``{item: value}`` map with every value coerced to ``str`` (enum items
        come back as their label string, value items as integers). Used by
        AsidScene to read `SID Detected Socket 1/2` (prefer-physical policy) and
        to snapshot the `SID Addressing` map so teardown can restore it. Raises
        ``requests.RequestException`` on transport/HTTP failure; callers treat
        the read as best-effort."""
        from urllib.parse import quote

        url = f"{self.base_url}/v1/configs/{quote(category)}"
        r = self.session.get(url, timeout=timeout)
        r.raise_for_status()
        body = r.json()
        inner = body.get(category, {}) if isinstance(body, dict) else {}
        return {k: str(v) for k, v in inner.items()} if isinstance(inner, dict) else {}

    def get_device_info(self, *, timeout: float = 3.0) -> dict[str, str]:
        """Read device identity LIVE over the REST API.

        ``GET /v1/info`` → ``{"product": ..., "firmware_version": ...,
        "fpga_version": ..., "hostname": ..., "unique_id": ..., ...}``. The
        ``unique_id`` (e.g. ``"5D327C"``) is a stable per-unit identifier —
        unlike the host/IP, it survives a DHCP re-lease — so
        :mod:`c64cast.audio.dac_calibration` uses it to key a system's calibrated
        DAC table. Raises ``requests.RequestException`` on transport/HTTP
        failure (older firmware without ``/v1/info``, unreachable device);
        callers treat the read as best-effort and fall back to a host-keyed
        name."""
        url = f"{self.base_url}/v1/info"
        r = self.session.get(url, timeout=timeout)
        r.raise_for_status()
        body = r.json()
        return {k: str(v) for k, v in body.items()} if isinstance(body, dict) else {}

    def describe_device(self) -> str:
        """This unit's identity for the connect-time log, from ``GET /v1/info``:
        ``"Ultimate II+ 5D327C (firmware 3.14d, FPGA 122)"``. Empty when the
        device won't answer (older firmware without ``/v1/info``).

        ``product`` is the only thing that distinguishes a U64 from a U2+ over
        this API, and the two differ in which config categories they expose — so
        without this line a config-surface failure reads as a bare 404."""
        try:
            info = self.get_device_info()
        except requests.RequestException:
            log.debug("device identity read failed", exc_info=True)
            return ""
        parts = [info.get("product") or "Ultimate"]
        if unique_id := info.get("unique_id"):
            parts.append(unique_id)
        versions = [
            f"{label} {info[key]}"
            for label, key in (("firmware", "firmware_version"), ("FPGA", "fpga_version"))
            if info.get(key)
        ]
        if versions:
            parts.append(f"({', '.join(versions)})")
        return " ".join(parts)

    def get_config_categories(self, *, timeout: float = 3.0) -> list[str]:
        """The config categories this device's firmware exposes
        (``GET /v1/configs`` → ``{"categories": [...]}``) — the capability
        contract `refine_capabilities` checks the multi-SID surface against.
        Raises ``requests.RequestException`` on transport/HTTP failure; an
        unrecognized response shape returns ``[]``."""
        r = self.session.get(f"{self.base_url}/v1/configs", timeout=timeout)
        r.raise_for_status()
        body = r.json()
        categories = body.get("categories") if isinstance(body, dict) else None
        if not isinstance(categories, list):
            return []
        return [category for category in categories if isinstance(category, str)]

    def refine_capabilities(self) -> None:
        """One cheap REST call resolving which config surfaces this device
        actually carries: revoke the U64 multi-SID surface the family profile
        claims optimistically when its categories are absent (Ultimate II+),
        and grant the U2+ emulated-stereo-SID surface and the U64 System Mode
        surface when their categories are present. Category presence, not the
        ``product`` string, is the test:
        the category list is the actual contract and tracks firmware
        differences within one product, the product string is presentation.

        A failed or unrecognizable read keeps the profile untouched: every
        SID config call site already absorbs a missing surface per-call, but
        nothing could absorb SID config wrongly *disabled* on a healthy U64
        over a transient read error. The emusid flag stays conservative-False
        on such a run — an unprobed run behaves exactly as before the flag
        existed."""
        try:
            categories = set(self.get_config_categories())
        except (requests.RequestException, ValueError) as e:
            log.debug("capability probe: /v1/configs unreadable (%s) — keeping optimism", e)
            return
        if not categories:
            log.debug("capability probe: unrecognized /v1/configs shape — keeping optimism")
            return

        has_emusid = EMUSID_MIXER_CATEGORY in categories
        if has_emusid != self.profile.supports_emusid_mixer:
            self.profile = replace(self.profile, supports_emusid_mixer=has_emusid)

        has_system_mode = SYSTEM_MODE_CATEGORY in categories
        if has_system_mode != self.profile.supports_system_mode:
            self.profile = replace(self.profile, supports_system_mode=has_system_mode)

        missing = [c for c in SID_CONFIG_CATEGORIES if c not in categories]
        if not missing or not self.profile.supports_sid_config:
            return
        self.profile = replace(self.profile, supports_sid_config=False)
        if has_emusid:
            log.info(
                "this device has no multi-SID config surface (no %s) — SID "
                "socket/UltiSID routing and chip-model matching are "
                "unavailable; using the emulated stereo-SID surface (%s) for "
                "snoop routing, panning and volume instead",
                ", ".join(missing),
                EMUSID_MIXER_CATEGORY,
            )
        else:
            log.info(
                "this device has no multi-SID config surface (no %s) — SID "
                "routing, chip-model matching and mixer control are "
                "unavailable; tunes play on whatever answers their addresses",
                ", ".join(missing),
            )

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

    def _launch_sid_player(self, launch: _SidLaunch) -> bool:
        """Ultimate kick: DMA the SID payload + player MC + re-INIT stub, flush
        so all three land, then POST a `10 SYS <player_base>` PRG to the REST
        run_prg runner. run_prg soft-resets the C64 (RAM preserved — the player
        MC at $C300 survives) and RUNs the stub; BASIC's SYS jumps to the player
        MC, which installs the IRQ and spins forever (never re-entering BASIC).

        Blanks the display first (same guard as `reset()`): run_prg's reset has
        its own reset-latency window during which the VIC still holds the
        OUTGOING scene's mode/bank, so without this a bitmap/hires scene (e.g.
        another waveform or a video scene) flashes its leftover bitmap RAM until
        the kernal reinitializes VIC and, in turn, `_setup_hires()` re-engages
        the scope. This is the same class of glitch `reset()`'s pre-blank fixes;
        `run_prg` here is a parallel reset path that needs the same guard.

        `avoid` is unused here (no trampoline to place). `defer_audio` is ignored:
        run_prg is a synchronous reset+RUN that also re-inits VIC to text mode, so
        there's no loaded-but-silent window to hold — audio starts here. Returns
        True so `run_sid_player` runs the standard finalize (timestamp + divider).
        WaveformScene's `begin_sid_audio()` is then a no-op, and it (re)asserts the
        bitmap display *after* this call as it always has."""
        self.blank_display()
        self._write_sid_blobs(launch)
        self.flush()
        basic_stub = _build_basic_sys_stub(launch.layout.player_base)
        url = f"{self.base_url}{U64_API.RUN_PRG}"
        try:
            r = self.session.post(
                url,
                files={"file": ("sidplayer.prg", basic_stub)},
                timeout=launch.timeout,
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

    def _kick_char_rom_dump(self, stub_addr: int, timeout: float) -> None:
        """Ultimate kick: POST a `10 SYS <stub_addr>` PRG to the REST run_prg
        runner, exactly like the SID player's. run_prg soft-resets the C64
        (RAM preserved — RAMTAS restores every byte its memory-size scan
        probes, which is why the stub at $8100 and the landing zone at $C000
        both survive) and RUNs the stub, whose `SYS` calls into our copy.

        The reset is also why the display is blanked first (same guard as
        `reset()`: the VIC holds the outgoing scene's mode/bank through the
        reset-latency window and would otherwise flash leftover bitmap RAM),
        and why the BASIC clear loop is re-established afterwards — the caller
        gets the machine back in the idle state it handed over."""
        self.blank_display()
        self.flush()
        url = f"{self.base_url}{U64_API.RUN_PRG}"
        try:
            r = self.session.post(
                url,
                files={"file": ("chargen.prg", _build_basic_sys_stub(stub_addr))},
                timeout=timeout,
            )
            r.raise_for_status()
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                raise RuntimeError(
                    f"U64 endpoint {url} returned 404 — run_prg is "
                    "required for the character-ROM dump."
                ) from e
            raise

    def dump_char_rom(self, timeout: float = 10.0) -> bytes:
        """Dump the character ROM, then put the machine back in the BASIC
        clear loop the kick's reset knocked it out of (see
        `_kick_char_rom_dump`). Restored even on failure — a half-dumped
        machine sitting at a READY banner is not a state any caller wants."""
        try:
            return super().dump_char_rom(timeout=timeout)
        finally:
            self.run_basic_clear_loop()

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
