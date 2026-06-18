"""Centralized C64 hardware constants — addresses, registers, magic numbers.

Pulling all the bare hex addresses into one module makes the rest of the
code self-documenting (you can grep for `VIC.D018_VIC_BANK` instead of
`"d018"`), and makes porting to other Commodore variants tractable.

Group constants by chip / subsystem:
  * VIC-II (`VIC`) — video registers ($D000-$D02E) and memory areas
  * SID (`SID`) — sound chip registers ($D400-$D41C) and per-voice offsets
  * CIA1, CIA2 (`CIA1`, `CIA2`) — the two 6526 timer/IO chips
  * KERNAL — useful kernal ROM entry points
  * IRQ / NMI — vector addresses + helpers
  * SCREEN — screen RAM, color RAM, character ROM locations
  * U64_API — REST endpoint paths (relative to base URL)
"""

from __future__ import annotations

from typing import Final, Literal

# ---------------------------------------------------------------------------
# VIC-II — video chip
# ---------------------------------------------------------------------------


class VIC:
    BASE: Final = 0xD000

    # Sprite X/Y position registers ($D000-$D00F: X0, Y0, X1, Y1, ..., X7, Y7).
    SPRITE_X0: Final = 0xD000
    SPRITE_Y0: Final = 0xD001
    SPRITE_X_MSB: Final = 0xD010  # high bit of each sprite's X position

    # Screen control / mode registers.
    D011_CONTROL_1: Final = 0xD011  # bit 7 = raster MSB, 4 = display enable, etc.
    D011_DISPLAY_ENABLE: Final = 0x10  # bit 4 (DEN): 0 = screen blanked to border
    D012_RASTER: Final = 0xD012  # current raster line (read) / IRQ line (write)
    D015_SPRITE_EN: Final = 0xD015  # bit per sprite — enable/disable
    D016_CONTROL_2: Final = 0xD016  # bit 4 = multicolor mode, 3 = 38/40-col
    D017_SPRITE_YE: Final = 0xD017  # vertical expansion enable per sprite
    D018_MEMORY: Final = 0xD018  # screen-mem + char-set / bitmap base
    D019_IRQ_FLAGS: Final = 0xD019  # IRQ status; write to ack
    D01A_IRQ_ENABLE: Final = 0xD01A  # IRQ enable mask (raster, sprite, ...)
    D01B_SPRITE_PRI: Final = 0xD01B  # sprite priority (vs background)
    D01C_SPRITE_MC: Final = 0xD01C  # multicolor sprite enable per sprite
    D01D_SPRITE_XE: Final = 0xD01D  # horizontal expansion enable per sprite

    # Color registers.
    D020_BORDER: Final = 0xD020
    D021_BG0: Final = 0xD021  # background color 0 (the main bg)
    D022_BG1: Final = 0xD022
    D023_BG2: Final = 0xD023
    D024_BG3: Final = 0xD024
    D025_SPRITE_MC1: Final = 0xD025  # shared sprite multicolor 1
    D026_SPRITE_MC2: Final = 0xD026  # shared sprite multicolor 2
    SPRITE_COLOR_0: Final = 0xD027  # ..D02E: per-sprite color

    # Raster IRQ bit in $D019 / $D01A.
    IRQ_RASTER: Final = 0x01

    # Sprite pointers live in the last 8 bytes of screen RAM (default bank).
    SPRITE_POINTERS: Final = 0x07F8


# ---------------------------------------------------------------------------
# SID — sound chip
# ---------------------------------------------------------------------------


class SID:
    BASE: Final = 0xD400
    BYTES_PER_VOICE: Final = 7
    N_VOICES: Final = 3

    # Per-voice offsets from voice base.
    OFF_FREQ_LO: Final = 0
    OFF_FREQ_HI: Final = 1
    OFF_PW_LO: Final = 2
    OFF_PW_HI: Final = 3
    OFF_CONTROL: Final = 4
    OFF_AD: Final = 5
    OFF_SR: Final = 6

    # Filter + master volume.
    FC_LO: Final = 0xD415
    FC_HI: Final = 0xD416
    RES_FILT: Final = 0xD417
    MODE_VOL: Final = 0xD418

    # Read-only ADC + osc3 / env3 outputs.
    POT_X: Final = 0xD419
    POT_Y: Final = 0xD41A
    OSC3: Final = 0xD41B
    ENV3: Final = 0xD41C

    # Control register bits.
    GATE: Final = 0x01
    SYNC: Final = 0x02
    RING_MOD: Final = 0x04
    TEST: Final = 0x08
    WAVE_TRIANGLE: Final = 0x10
    WAVE_SAWTOOTH: Final = 0x20
    WAVE_PULSE: Final = 0x40
    WAVE_NOISE: Final = 0x80

    @classmethod
    def voice_base(cls, voice_idx: int) -> int:
        """Return the base address of voice `voice_idx` (0..2)."""
        return cls.BASE + voice_idx * cls.BYTES_PER_VOICE


# ---------------------------------------------------------------------------
# CIA1 / CIA2 — the two 6526 timer/IO chips
# ---------------------------------------------------------------------------


class CIA1:
    BASE: Final = 0xDC00
    # PORT_A ($DC00) = keyboard column-select output / joystick port 2 input;
    # PORT_B ($DC01) = keyboard row input / joystick port 1 input. Joystick
    # lines are active-low: a pressed direction/fire pulls its bit to 0, idle
    # reads all-high. Bits 0-4 = up/down/left/right/fire. The LauncherScene
    # polls these to detect player input (see scenes.LauncherScene). NB: while
    # a program runs its own keyboard-matrix scan it drives PORT_A as output,
    # so reads can momentarily race that scan — input detection is best-effort.
    PORT_A: Final = 0xDC00
    PORT_B: Final = 0xDC01
    JOY_UP: Final = 0x01
    JOY_DOWN: Final = 0x02
    JOY_LEFT: Final = 0x04
    JOY_RIGHT: Final = 0x08
    JOY_FIRE: Final = 0x10
    JOY_MASK: Final = 0x1F  # low 5 bits = the four directions + fire
    TIMER_A_LO: Final = 0xDC04
    TIMER_A_HI: Final = 0xDC05
    ICR: Final = 0xDC0D


class CIA2:
    BASE: Final = 0xDD00
    # PORT_A bit 0-1 = inverted VIC bank select (00=bank 3, 11=bank 0). The
    # upper bits drive the serial bus / RS-232 outputs; c64cast doesn't
    # use those, so we write the whole byte and accept clobbering them.
    PORT_A: Final = 0xDD00
    TIMER_A_LO: Final = 0xDD04
    TIMER_A_HI: Final = 0xDD05
    ICR: Final = 0xDD0D

    # Whole-byte values for PORT_A that select a given VIC bank. The 0x97
    # base (1001 0111) keeps serial-bus output lines high (idle, no device
    # active) which matches the kernal's post-init state; bits 0-1 are
    # inverted from the bank number (bank 0 = 11, bank 1 = 10, bank 2 = 01).
    # The REU-staged PETSCII / Blank double-buffer swaps between bank 0
    # (default) and bank 2 — the only two banks with kernal char-ROM mapped
    # at $1000/$9000, which char modes need. Those modes must avoid bank 1
    # because the audio ring lives there ($4000-$5FFF).
    #
    # The waveform scene is the exception: it's bitmap-only (no char-ROM
    # dependency) and stops the audio ring at setup (the SID plays on the
    # real chip), so bank 1 is a free display target there. It selects
    # PORT_A_BANK_1 for tunes whose payload/footprint occupies banks 0 and 2
    # (e.g. Galway's Times of Lore subtunes 2-11). See waveform._DISPLAY_BANKS.
    PORT_A_BANK_0: Final = 0x97  # bits 0-1 = 11 → VIC bank 0 ($0000-$3FFF)
    PORT_A_BANK_1: Final = 0x96  # bits 0-1 = 10 → VIC bank 1 ($4000-$7FFF)
    PORT_A_BANK_2: Final = 0x95  # bits 0-1 = 01 → VIC bank 2 ($8000-$BFFF)


# ---------------------------------------------------------------------------
# VIC bank layout — char-mode addresses within each VIC bank
# ---------------------------------------------------------------------------
# When VIC bank N is selected via CIA2.PORT_A, the addresses VIC actually
# fetches from are bank-base + the offset encoded in $D018:
#   $D018 = $14 → matrix at offset $0400, char gen at offset $1000.
# Banks 0 ($0000-$3FFF) and 2 ($8000-$BFFF) are the only ones with kernal
# char-ROM mapped at the $1000 offset. PETSCII / Blank scenes set
# $D018 = $14 so both banks render with the same matrix+chars layout.
#
# For the REU-staged display modes, screens are renderable into either
# bank — bank 0 uses $0400, bank 2 uses $8400. Double-buffering swaps
# CIA2.PORT_A between PORT_A_BANK_0 and PORT_A_BANK_2.


class VIC_BANK_0:
    BASE: Final = 0x0000
    SCREEN: Final = 0x0400  # $D018 matrix nibble = 1
    BITMAP: Final = 0x2000  # $D018 bitmap nibble = 4
    CHAR_ROM: Final = 0x1000  # $D018 char nibble = 4 (kernal-mapped)


class VIC_BANK_2:
    BASE: Final = 0x8000
    SCREEN: Final = 0x8400  # same $D018 ($14) as bank 0
    BITMAP: Final = 0xA000  # same $D018 ($18) hires offset
    CHAR_ROM: Final = 0x9000  # kernal-mapped char-ROM in bank 2


# ---------------------------------------------------------------------------
# REU (RAM Expansion Unit) — REC controller at $DF00-$DF0A
# ---------------------------------------------------------------------------
# The U64 emulates a 17xx-style REU with one DMA-capable controller mapped
# at $DF00-$DF0A. Triggers move bytes between REU FPGA SRAM and the C64
# bus (main RAM or I/O space) at ~1 byte per cycle while halting the 6510.
# c64cast uses this for:
#   * audio: a kernal-IRQ-triggered pump streams pre-staged samples from
#     REU into the audio ring (see [audio.py] REU_IRQ_HANDLER).
#   * video (REU-staged display modes): the host pre-stages frame data
#     into REU via socket DMA opcode 0xFF07 (REUWRITE, no bus halt), then
#     triggers REU→main DMAs to drop the frame into screen RAM.
#
# The REC is a single shared resource — c64cast serializes audio + video
# REU usage at the scene level (REU video opt-in cannot coexist with REU
# audio in the current slice; mutual exclusion is enforced at scene setup).


class REU:
    BASE: Final = 0xDF00
    STATUS: Final = 0xDF00  # read-only status / version
    COMMAND: Final = 0xDF01  # write to trigger a DMA
    C64_ADDR_LO: Final = 0xDF02  # main RAM dest/src LO
    C64_ADDR_HI: Final = 0xDF03  # main RAM dest/src HI
    REU_ADDR_LO: Final = 0xDF04  # REU offset LO (24-bit)
    REU_ADDR_MI: Final = 0xDF05  # REU offset MI
    REU_ADDR_HI: Final = 0xDF06  # REU offset HI
    LENGTH_LO: Final = 0xDF07  # transfer length LO (decremented in flight!)
    LENGTH_HI: Final = 0xDF08  # transfer length HI
    IRQ_MASK: Final = 0xDF09
    ADDR_CONTROL: Final = 0xDF0A  # bits 6-7: hold-src, hold-dst; 0=both auto-inc

    # COMMAND byte composition. exec(bit 7) + FF00-disable(bit 4) + direction(bits 0-1).
    # autoload(bit 5) = OFF so src/dst auto-increment carries across triggers.
    CMD_EXEC: Final = 0x80
    CMD_FF00_OFF: Final = 0x10
    CMD_DIR_C64_TO_REU: Final = 0x00
    CMD_DIR_REU_TO_C64: Final = 0x01  # REU → main RAM (the c64cast direction)
    CMD_DIR_SWAP: Final = 0x02
    CMD_DIR_VERIFY: Final = 0x03
    CMD_FETCH_EXEC: Final = CMD_EXEC | CMD_FF00_OFF | CMD_DIR_REU_TO_C64  # $91


# ---------------------------------------------------------------------------
# Kernal ROM entry points + IRQ/NMI vectors
# ---------------------------------------------------------------------------


class KERNAL:
    IRQ_HANDLER: Final = 0xEA31  # BASIC IRQ handler tail; chain to here
    IRQ_RETURN: Final = 0xEA81  # kernal IRQ register-restore + RTI (lean
    # exit: skip the kernal tail, just ack+RTI)
    DEFAULT_NMI: Final = 0xFE47  # default NMI vector target
    CHROUT: Final = 0xFFD2  # output a char to current channel
    CLR_HOME: Final = 0x93  # PETSCII code for clear-screen + home


class VECTORS:
    """RAM-shadowed jump vectors (kernal jumps through these so user code
    can patch them)."""

    IRQ: Final = 0x0314  # IRQ vector (lo/hi)
    NMI: Final = 0x0318  # NMI vector (lo/hi)


class CPU:
    """6510 processor I/O port at $0001 — bits 0-2 (LORAM/HIRAM/CHAREN)
    control which ROMs and I/O are visible in the memory map."""

    PORT: Final = 0x0001  # 6510 processor port (bank control)
    # $37 = power-on default: BASIC ROM + KERNAL ROM + I/O all mapped.
    PORT_DEFAULT: Final = 0x37
    # $36 = BASIC ROM banked out (LORAM=0); KERNAL ROM + I/O still mapped.
    # The minimum config for running SID tunes whose code/data live under
    # BASIC ROM while keeping the $EA31 kernal IRQ chain + $D4xx/CIA I/O.
    PORT_BASIC_OUT: Final = 0x36
    # $35 = BASIC + KERNAL ROM banked out (HIRAM=0 too); I/O still mapped.
    # For code/data under KERNAL ROM ($E000-$FFFF). Used by the SID player's
    # per-call banking (mirrors the U64 firmware's getBank).
    PORT_KERNAL_OUT: Final = 0x35
    # $34 = all ROM + I/O banked out (CHAREN=0): full RAM, no $Dxxx I/O.
    # For code/data living under the I/O window ($D000-$DFFF) that must be
    # read/written as RAM.
    PORT_IO_OUT: Final = 0x34


class ROM:
    """ROM windows in the C64 memory map (the bytes a ROM covers when
    mapped via CPU.PORT_DEFAULT). Used to decide when a SID tune's RAM
    is hidden behind a ROM and needs the bank config adjusted."""

    BASIC_LO: Final = 0xA000  # BASIC ROM $A000-$BFFF
    BASIC_HI: Final = 0xC000  # exclusive
    KERNAL_LO: Final = 0xE000  # KERNAL ROM $E000-$FFFF
    KERNAL_HI: Final = 0x10000  # exclusive


# ---------------------------------------------------------------------------
# Screen layout
# ---------------------------------------------------------------------------


class SCREEN:
    RAM: Final = 0x0400  # default screen RAM ($0400-$07E7)
    COLOR_RAM: Final = 0xD800  # color RAM ($D800-$DBE7)
    BITMAP: Final = 0x2000  # hires bitmap area ($2000-$3F40)
    CHAR_ROM: Final = 0xD000  # character ROM (banked)

    W_CHARS: Final = 40
    H_CHARS: Final = 25
    N_CELLS: Final = 1000  # 40 * 25
    BITMAP_BYTES: Final = 8000  # 320 * 200 / 8
    BITMAP_W: Final = 320
    BITMAP_H: Final = 200

    # Keyboard scratch bytes.
    LAST_KEY: Final = 0x00C5  # matrix code of last key pressed (64 = none)
    CUR_KEY: Final = 0x00CB  # matrix code of key currently down (64 = none)
    KB_BUFFER_LEN: Final = 0x00C6
    KB_BUFFER: Final = 0x0277
    MODIFIERS: Final = 0x028D  # bit 1 = COMMODORE, 0 = SHIFT, 2 = CTRL
    CASE_SWITCH: Final = 0x0291  # bit 7 = 1 disables the C= + SHIFT charset toggle

    # Editor scratch bytes.
    BLNSW: Final = 0x00CC  # cursor blink switch: 0 = blink, non-0 = suppress

    # Common screen codes (what goes into $0400 — not the same as PETSCII
    # for chars above 0x40; e.g. PETSCII '@' = 0x40 but screen code 0x00).
    SC_SPACE: Final = 0x20  # blank cell — invisible against bg
    SC_FULL_BLOCK: Final = 0xA0  # inverse space — fully filled in FG color


class KEY:
    """Keyboard matrix scan codes — the value the kernal writes to
    $00C5/$00CB (SCREEN.LAST_KEY/CUR_KEY); 64 = no key. These are NOT
    PETSCII or screen codes. Only the handful the on-C64 menu navigates."""

    NONE: Final = 64
    RETURN: Final = 1
    CRSR_RIGHT: Final = 2
    CRSR_DOWN: Final = 7
    SPACE: Final = 60


# ---------------------------------------------------------------------------
# Raster IRQ helpers
# ---------------------------------------------------------------------------

# Raster line where vblank starts on both PAL and NTSC — safe to commit
# VIC register changes here without tearing visible scan lines.
RASTER_VBLANK_LINE: Final = 0xF8


# ---------------------------------------------------------------------------
# Ultimate-64 REST API endpoints (relative to base URL)
# ---------------------------------------------------------------------------


class U64_API:
    # /v1/machine:writemem is intentionally absent — writes go over Socket
    # DMA, not REST. /v1/runners:sidplay is intentionally absent — the
    # firmware UI it draws hides VIC output (see api.run_sid_player).
    READ_MEM: Final = "/v1/machine:readmem"
    RESET: Final = "/v1/machine:reset"
    RUN_PRG: Final = "/v1/runners:run_prg"
    RUN_CRT: Final = "/v1/runners:run_crt"


# ---------------------------------------------------------------------------
# System clocks (in Hz) for NTSC + PAL.
# ---------------------------------------------------------------------------

CLOCK_NTSC: Final = 1022727
CLOCK_PAL: Final = 985248


def cpu_clock(system: str) -> int:
    """Return the CPU clock in Hz for the given system ('NTSC' or 'PAL')."""
    return CLOCK_NTSC if system.upper() == "NTSC" else CLOCK_PAL


# ---------------------------------------------------------------------------
# NMI audio sample-rate safety budget.
# ---------------------------------------------------------------------------
# The $D418 DAC NMI handler (audio.NMI_ROUTINE) pulls one sample per fire. It
# completes in 41 cycles on the fast path and 81 cycles when a VIC-II badline
# steals 40 cycles mid-handler. The NMI also can't be serviced until the
# in-progress instruction finishes (up to ~7 cycles). If the sample PERIOD
# (= cpu_clock / sample_rate) is shorter than the handler can complete, NMIs
# queue and fire back-to-back — samples stretch and pitch drops (the measured
# 16 kHz failure shifted a 440 Hz tone to 421 Hz; see the audio.py docstring).
#
# PAL is tighter than NTSC: its slower clock means fewer cycles per period at
# the same rate, so the safe ceiling is lower (~10.5 kHz PAL vs ~11.6 kHz NTSC).
# HW-measured 2026-06-15: NTSC@11025 and PAL@10500 both ran with the NMI
# consumer tracking ~98% of the configured rate (no overrun); 8 kHz→~10.5 kHz
# recovers the 4-5.5 kHz fricative band the old 4 kHz Nyquist discarded.
NMI_HANDLER_WORST_CYCLES: Final = 81  # 41 work + 40 badline steal
NMI_ENTRY_LATENCY_CYCLES: Final = 7  # worst-case wait for the in-progress instr
NMI_SAFE_MIN_PERIOD_CYCLES: Final = NMI_HANDLER_WORST_CYCLES + NMI_ENTRY_LATENCY_CYCLES  # 88


def max_safe_sample_rate(system: str) -> int:
    """Highest sample rate whose NMI period stays at/above the safe minimum
    (handler worst case + entry latency) for `system`. ~11.6 kHz NTSC / ~11.1
    kHz PAL — but ears/HW put the comfortable default lower (10.5 kHz)."""
    return int(cpu_clock(system) // NMI_SAFE_MIN_PERIOD_CYCLES)


def nmi_rate_safety(system: str, sample_rate: int) -> tuple[Literal["ok", "warn", "error"], str]:
    """Classify an audio sample rate against the NMI handler's cycle budget for
    `system`. Returns ``(level, message)`` where level is "ok" | "warn" |
    "error" — pure (no I/O), so config validation, --doctor, and tests share
    one source of truth. "error" = period below the handler worst case (NMIs
    WILL queue, pitch drops); "warn" = inside entry-latency margin (may glitch
    under badline-heavy scenes); "ok" = clear."""
    if sample_rate <= 0:
        return ("error", f"sample_rate must be positive, got {sample_rate}")
    period = cpu_clock(system) / sample_rate
    safe_max = max_safe_sample_rate(system)
    if period < NMI_HANDLER_WORST_CYCLES:
        return (
            "error",
            f"sample_rate {sample_rate} Hz → NMI period {period:.0f} cycles on "
            f"{system}, below the {NMI_HANDLER_WORST_CYCLES}-cycle handler worst "
            f"case: NMIs queue and pitch drops. Max safe on {system} ≈ {safe_max} Hz.",
        )
    if period < NMI_SAFE_MIN_PERIOD_CYCLES:
        return (
            "warn",
            f"sample_rate {sample_rate} Hz → NMI period {period:.0f} cycles on "
            f"{system}, within entry-latency margin of the {NMI_HANDLER_WORST_CYCLES}-"
            f"cycle handler — may glitch under badline-heavy scenes. Safe max ≈ {safe_max} Hz.",
        )
    return (
        "ok",
        f"sample_rate {sample_rate} Hz → NMI period {period:.0f} cycles on "
        f"{system} (safe; handler ≤ {NMI_HANDLER_WORST_CYCLES}).",
    )


# ---------------------------------------------------------------------------
# Region IDs for the dirty-cache in Ultimate64API.write_region.
# ---------------------------------------------------------------------------
# Each ID identifies a logical write region; the cache keys by ID (not by
# address) so a mode switch reusing the same address gets a clean baseline
# via api.invalidate_cache(). IDs must be unique across all callers — when
# adding a new region, claim a fresh slot here so collisions are visible at
# definition time rather than as silent cache corruption mid-run.


class RegionID:
    # Char-mode displays (modes.py, interstitial.py, midi_scene.py).
    SCREEN: Final = 1  # $0400 (1000 bytes)
    COLOR: Final = 2  # $D800 (1000 bytes)
    BITMAP: Final = 3  # $2000 (8000 bytes)

    # Waveform scene (waveform.py). 10 IDs reserved per base for per-voice
    # offsets — use `RegionID.WAVE_BITMAP + voice_idx` for voice-specific
    # writes, pass the base value for whole-region writes.
    WAVE_BITMAP: Final = 4000  # +0..+9
    WAVE_SCREEN: Final = 4010  # +0..+9
    WAVE_COLOR: Final = 4020  # +0..+9

    # Waveform-scene metadata rows (hires display only). Each text row is
    # one cache entry for the bitmap glyphs + one for the screen-RAM color
    # nibble. Using stable IDs across paints lets the delta cache absorb
    # unchanged columns on a SHIFT-driven song change (only the song-number
    # digits move; everything else is identical bytes).
    WAVE_TITLE_BITMAP: Final = 4030
    WAVE_TITLE_SCREEN: Final = 4031
    WAVE_META_BITMAP: Final = 4032
    WAVE_META_SCREEN: Final = 4033
    # One-time full screen-matrix clear at _setup_hires — zeroes the spacer
    # rows the per-voice/title/meta paints don't cover, so a relocated
    # (VIC bank 2) display doesn't show uninitialized-RAM garbage there.
    WAVE_SCREEN_CLEAR: Final = 4034

    # On-C64 menu overlay (overlays/menu.py). Per-panel-row IDs (+row offset,
    # panel is at most 25 rows) so the delta cache absorbs unchanged rows
    # between repaints. Bitmap displays use ROW_BITMAP + ROW_SCREEN (color in
    # the screen nibble); char displays use ROW_SCREEN + ROW_COLOR.
    MENU_ROW_BITMAP: Final = 5000  # +row 0..24
    MENU_ROW_SCREEN: Final = 5100  # +row 0..24
    MENU_ROW_COLOR: Final = 5200  # +row 0..24
