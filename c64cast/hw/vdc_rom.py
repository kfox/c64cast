"""The C128-mode cartridge ROM: a resident 8502 loop that blits host RAM to VDC
video RAM.

## Why this exists

``hw/vdc.py`` drives the VDC entirely from the host, one porthole access per
byte, and every one of those is a TeensyROM+ round trip — measured at 5.5 KB/s,
so a 24 KB frame takes about 4.4 seconds. The VDC's own block fill and copy are
faster (45 and 41 KB/s) but they can only move bytes *within* video RAM; they
cannot get host pixels in.

The fix is to stop making the host do it. The 8502 can reach the porthole at
memory speed, so if the frame is already in C128 RAM the copy costs about 21
cycles per byte instead of a serial round trip. The host's job becomes a bulk
DMA into RAM — which the TeensyROM+ link is good at — and the resident loop does
the porthole work locally.

## Getting there: the cartridge

``vdc.build_c128_crt`` wraps this ROM so a TeensyROM+ launches it as
``rtBinC128`` (GAME and EXROM both deasserted), which boots the C128 into
**native 128 mode** and autostarts the cartridge at ``$8000``. That is also why
this path is worth having beyond speed: the machine comes up in a known state
every time, instead of inheriting whatever the TR menu or a previous run left
behind.

## The banking dance

The cartridge lives at ``$8000``, but the resident loop wants that space for
RAM. So the ROM's first act is to copy the loop into RAM at
:data:`RESIDENT_ADDR` and ``JMP`` there; the loop's own first act is to set the
MMU to ``$3E`` — RAM bank 0 everywhere, I/O still mapped — which banks the
cartridge out from under itself. That is safe only because the copy already
happened and ``$0000-$3FFF`` is RAM in every MMU configuration, so the
instruction after the ``STA $FF00`` still fetches from the same bytes.

Banking out the KERNAL leaves ``$FFFA``-``$FFFF`` as plain RAM, so the loop
points the NMI and IRQ vectors at an ``RTI`` before anything can use them —
otherwise the RESTORE key vectors through uninitialized RAM.

## The mailbox

Host and loop rendezvous through a fixed block at :data:`MAILBOX` (see the
``MAIL_*`` offsets). The host writes the parameters, then writes
:data:`MAIL_CMD` **last and separately** — the command byte is the commit, so it
must not share a DMA with the parameters it commits.

:data:`MAIL_BEAT` is incremented on every pass of the idle loop. It is the
cheapest possible proof of life: read it twice, and if it changed the ROM booted
and is running. On a bench with no RGBI capture that is the difference between
verifying the boot path and guessing at it.

While idle the loop touches nothing but RAM, so the host is free to drive the
porthole directly between commands.

## Clock speed

Everything runs at 1 MHz. Blitting at 2 MHz was tried first and rejected: it is
faster (43 KB/s against 18) but it wedges. Five 24000-byte blits on an 8563
R8/R9 gave four completions and one hang with the heartbeat frozen, the CPU
stuck in the ``BIT $D600 / BPL`` wait, needing a reset to recover. A blit that
is twice as fast and occasionally costs the machine is not a trade worth having.

That wait loop is not optional either. Dropping it runs at 71 KB/s (1 MHz) or
147 (2 MHz) and corrupts every sample chunk of the result: the VDC silently
discards a porthole write that lands while it is busy, so the frame comes back
missing bytes rather than late.
"""

from __future__ import annotations

from typing import Final

from c64cast.hw import vdc
from c64cast.hw.asm6502 import assemble, labels_of

# ---------------------------------------------------------------------------
# Memory map
# ---------------------------------------------------------------------------

CART_ADDR: Final = 0x8000  # where the C128 maps external function ROM
RESIDENT_ADDR: Final = 0x2000  # RAM the loop is copied to and runs from
RESIDENT_MAX: Final = 0x0200  # the cartridge stub copies exactly two pages
MAILBOX: Final = 0x1000

MAIL_CMD: Final = MAILBOX + 0  # 0 = idle; written last, on its own
MAIL_ARG: Final = MAILBOX + 1  # VDC register number for CMD_VDC_REG
MAIL_DST_LO: Final = MAILBOX + 2  # VRAM destination; also the value for CMD_VDC_REG
MAIL_DST_HI: Final = MAILBOX + 3
MAIL_CNT_LO: Final = MAILBOX + 4  # byte count
MAIL_CNT_HI: Final = MAILBOX + 5
MAIL_SRC_LO: Final = MAILBOX + 6  # source address in C128 RAM
MAIL_SRC_HI: Final = MAILBOX + 7
MAIL_BEAT: Final = MAILBOX + 8  # free-running; proof the loop is alive
MAIL_DONE: Final = MAILBOX + 9  # incremented after each command completes

CMD_BLIT: Final = 0x01  # RAM -> VRAM, MAIL_CNT bytes
CMD_VDC_REG: Final = 0x02  # write MAIL_DST_LO to VDC register MAIL_ARG

#: Where the host should stage frames. Page-aligned and clear of both the
#: mailbox and the resident loop, with room for a 24000-byte frame and a second
#: buffer above it.
FRAMEBUF_ADDR: Final = 0x4000

#: Solid cyan (background 6, foreground 0) with a zeroed bitmap. The startup
#: screen is a flat color on purpose: it is unambiguous from across a room,
#: which garbage VRAM is not.
ATTR_INIT: Final = 0x60

#: MMU configuration: RAM bank 0 at every address, I/O still mapped at $D000.
MMU_ALL_RAM_IO: Final = 0x3E


# ---------------------------------------------------------------------------
# Source
# ---------------------------------------------------------------------------


def _vdc_register_table() -> str:
    """The bitmap-mode register program as ``reg, value`` pairs terminated by
    ``$FF``. Generated from :data:`vdc.BITMAP_640x200_REGS` so the cartridge and
    the host-driven path cannot disagree about the timing; ``$FF`` is free as a
    terminator because the VDC has only 37 registers."""
    pairs = [
        f"        .byte ${reg:02X},${value:02X}"
        for reg, value in sorted(vdc.BITMAP_640x200_REGS.items())
    ]
    return "vdctab:\n" + "\n".join(pairs) + "\n        .byte $FF\n"


def resident_source() -> str:
    """The 8502 loop, as source. Assembled at :data:`RESIDENT_ADDR`."""
    return f"""
; ---- entry: bank the cartridge out and take over the machine -------------
main:   LDA #${MMU_ALL_RAM_IO:02X}
        STA $FF00
        LDA #<isr
        STA $FFFA           ; NMI (RESTORE) - the KERNAL vectors are gone
        STA $FFFE           ; IRQ / BRK
        LDA #>isr
        STA $FFFB
        STA $FFFF
        LDA #$0B
        STA $D011           ; blank the 40-col screen; the VDC is the display
        LDA #$00
        STA $D020
        STA $D021
        JSR vdcinit
        JSR vclear
        LDA #$00
        STA ${MAIL_CMD:04X}
        STA ${MAIL_DONE:04X}

; ---- the idle loop -------------------------------------------------------
poll:   INC ${MAIL_BEAT:04X}
        LDA ${MAIL_CMD:04X}
        BEQ poll
        CMP #${CMD_BLIT:02X}
        BEQ c_blit
        CMP #${CMD_VDC_REG:02X}
        BEQ c_reg
        JMP ack             ; unknown command: acknowledge, do nothing
c_blit: JSR blit
        JMP ack
c_reg:  LDX ${MAIL_ARG:04X}
        LDA ${MAIL_DST_LO:04X}
        JSR vdcw
ack:    LDA #$00
        STA ${MAIL_CMD:04X}
        INC ${MAIL_DONE:04X}
        JMP poll

isr:    RTI

; ---- vdcw: write A to VDC register X ------------------------------------
vdcw:   STX $D600
vdcw1:  BIT $D600
        BPL vdcw1
        STA $D601
        RTS

; ---- vdcinit: run the register table ------------------------------------
vdcinit:LDX #$00
vi1:    LDY vdctab,X
        CPY #$FF
        BEQ vi2
        INX
        LDA vdctab,X
        INX
        STY $D600
vi3:    BIT $D600
        BPL vi3
        STA $D601
        JMP vi1
vi2:    RTS

; ---- vclear: blank the bitmap, flat-fill the attributes -----------------
vclear: LDA #$00
        STA $FB
        STA $FC
        LDA #${vdc.BITMAP_BYTES & 0xFF:02X}
        STA $FD
        LDA #${vdc.BITMAP_BYTES >> 8:02X}
        STA $FE
        LDA #$00
        JSR vfill
        LDA #${vdc.ATTR_BASE & 0xFF:02X}
        STA $FB
        LDA #${vdc.ATTR_BASE >> 8:02X}
        STA $FC
        LDA #${vdc.ATTR_BYTES & 0xFF:02X}
        STA $FD
        LDA #${vdc.ATTR_BYTES >> 8:02X}
        STA $FE
        LDA #${ATTR_INIT:02X}
        JSR vfill
        RTS

; ---- vfill: A -> $FD/$FE bytes of VRAM at $FB/$FC, via VDC block write ---
vfill:  STA $F9
        LDX #${vdc.R.V_SCROLL_CTRL:02X}
        LDA #$20
        JSR vdcw            ; R24 bit 7 clear = block WRITE
        LDX #${vdc.R.UPDATE_HI:02X}
        LDA $FC
        JSR vdcw
        LDX #${vdc.R.UPDATE_LO:02X}
        LDA $FB
        JSR vdcw
        LDX #${vdc.R.DATA:02X}
        LDA $F9
        JSR vdcw            ; the seed byte; R18/R19 auto-increment
        LDA $FD
        BNE vf0
        DEC $FE
vf0:    DEC $FD             ; the seed byte counts toward the total
vfl:    LDA $FD
        ORA $FE
        BEQ vfd
        LDA $FE
        BEQ vfs
        LDA #$FF            ; one R30 write runs at most 255 operations
        JMP vfg
vfs:    LDA $FD
vfg:    STA $F8
        LDX #${vdc.R.WORD_COUNT:02X}
        JSR vdcw
        LDY #$08            ; let the busy flag fall before believing a poll
vfw0:   DEY
        BNE vfw0
vfw:    BIT $D600
        BPL vfw
        LDA $FD
        SEC
        SBC $F8
        STA $FD
        LDA $FE
        SBC #$00
        STA $FE
        JMP vfl
vfd:    RTS

; ---- blit: MAIL_CNT bytes from MAIL_SRC to VRAM at MAIL_DST -------------
blit:   LDX #${vdc.R.UPDATE_HI:02X}
        LDA ${MAIL_DST_HI:04X}
        JSR vdcw
        LDX #${vdc.R.UPDATE_LO:02X}
        LDA ${MAIL_DST_LO:04X}
        JSR vdcw
        LDA #$1F
        STA $D600           ; select R31 once and stream through it
        LDA ${MAIL_SRC_LO:04X}
        STA $FB
        LDA ${MAIL_SRC_HI:04X}
        STA $FC
        LDA ${MAIL_CNT_LO:04X}
        STA $FD
        LDA ${MAIL_CNT_HI:04X}
        STA $FE
        LDY #$00
bpg:    LDA $FE
        BEQ brem
bp1:    BIT $D600
        BPL bp1
        LDA ($FB),Y
        STA $D601
        INY
        BNE bp1
        INC $FC
        DEC $FE
        JMP bpg
brem:   LDX $FD
        BEQ bdone
br1:    BIT $D600
        BPL br1
        LDA ($FB),Y
        STA $D601
        INY
        DEX
        BNE br1
bdone:  RTS

{_vdc_register_table()}
"""


def cartridge_source(resident: bytes) -> str:
    """The ``$8000`` stub: autostart header, copy ``resident`` into RAM, jump.

    The 10-byte header is the C128's autostart signature and is byte-for-byte
    what the reference ``C128_789010.crt`` carries — cold and warm ``JMP``s, the
    cartridge-type byte ``$02``, then plain ASCII ``"CBM"``."""
    body = "\n".join(
        "        .byte " + ",".join(f"${b:02X}" for b in resident[i : i + 16])
        for i in range(0, len(resident), 16)
    )
    return f"""
        JMP cold
        JMP cold
        .byte $02
        .text "CBM"
cold:   SEI
        LDX #$FF
        TXS
        CLD
        LDX #$00
ccopy:  LDA blob,X
        STA ${RESIDENT_ADDR:04X},X
        LDA blob+$0100,X
        STA ${RESIDENT_ADDR + 0x100:04X},X
        INX
        BNE ccopy
        JMP ${RESIDENT_ADDR:04X}
blob:
{body}
"""


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------


def build_resident() -> bytes:
    """The RAM-resident loop, padded to the :data:`RESIDENT_MAX` the cartridge
    stub copies."""
    image = assemble(resident_source(), RESIDENT_ADDR)
    if len(image) > RESIDENT_MAX:
        raise ValueError(
            f"resident loop is {len(image)} bytes; the cartridge stub copies "
            f"{RESIDENT_MAX}. Widen the copy loop in cartridge_source()."
        )
    return image.ljust(RESIDENT_MAX, b"\x00")


def resident_labels() -> dict[str, int]:
    """Addresses of the resident loop's routines, for tests and diagnostics."""
    return labels_of(resident_source(), RESIDENT_ADDR)


def build_rom() -> bytes:
    """The full 8 KiB cartridge image."""
    image = assemble(cartridge_source(build_resident()), CART_ADDR)
    if len(image) > 0x2000:
        raise ValueError(f"cartridge image is {len(image)} bytes; the bank is 8 KiB")
    return image.ljust(0x2000, b"\x00")


def build_crt(name: str = "c64cast VDC") -> bytes:
    """The cartridge as a ``.crt`` a TeensyROM+ will launch in C128 mode."""
    return vdc.build_c128_crt(build_rom(), name=name)
