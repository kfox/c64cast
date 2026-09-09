#!/usr/bin/env python3
"""Re-run the VDC porthole findings on somebody else's C128, and print a
transcript they can paste back.

Three results were measured on one 8563 R8/R9 and none of them can be told apart
from a fault in that particular machine until a second one has run the same
code:

1. **Single-bit VRAM errors after a polled blit** — roughly one byte per
   thousand comes back wrong, and on the machine measured so far the difference
   is *always* ``XOR $40``: bit 6, never any other bit, always 1 -> 0. A fault
   confined to one data line is exactly what a marginal VRAM chip or a bad
   solder joint looks like, so a second machine that shows zero of these, or
   shows them on other bits, moves this from "how the VDC behaves" to "how that
   unit behaves".
2. **The vertical-blanking window.** ``$D600`` bit 5 is high for about a quarter
   of the frame, which matches the 64 blanked scanlines of the 640x200 register
   program rather than the 4-line vsync pulse. The interesting part is that the
   ready poll still cannot be dropped inside it: an unpolled stream is clean for
   the first ~159 bytes and loses writes after that.
3. **How a lost write fails.** The update address does not auto-increment on a
   write the VDC discards, so everything after it lands one address early and
   the tail keeps whatever it held before. The result is a *shifted* stream, not
   a corrupted byte — invisible to a checksum of the source data, and a
   different mechanism from the single-bit errors in (1).

Everything here self-verifies over the link. **No RGBI capture card is needed**
and nothing has to be judged by eye.

    scripts/diags/vdc_second_machine.py                 # autodetect the TR+
    scripts/diags/vdc_second_machine.py --serial /dev/cu.usbmodemXXXX
    scripts/diags/vdc_second_machine.py --tcp <teensyrom-host>

Stages
  1. launch the probe cartridge, confirm the resident loop's heartbeat
  2. identity: chip revision, VRAM size, the live register program
  3. blanking duty cycle — what fraction of the frame has $D600 bit 5 set
  4. polled-blit integrity — the single-bit-error hunt (finding 1)
  5. unpolled stream inside blanking — window size and failure shape (2 and 3)

Resets the C128 on the way out unless --no-reset-exit.

## Why this carries its own cartridge

The resident loop below is a frozen copy, not ``hw/vdc_rom.py``'s. Cross-machine
numbers are only comparable if every machine ran the same bytes, and the shipped
ROM is expected to keep changing; freezing the probe here also keeps a
research-only command out of the ROM the app ships.
"""

from __future__ import annotations

import argparse
import collections
import contextlib
import statistics
import sys
import time
from typing import Final

import _diaglib  # noqa: F401  (path bootstrap: makes `import c64cast` work from any cwd)

from c64cast.hw import vdc, vdc_rom
from c64cast.hw.asm6502 import assemble
from c64cast.hw.teensyrom_dma import (
    DEFAULT_BAUD,
    DEFAULT_TCP_PORT,
    SerialTransport,
    TcpTransport,
    TRClient,
    TRError,
    autodetect_serial_port,
)

UPLOAD_PATH: Final = "/c64cast/vdcprobe.crt"

#: Research-only third command, on top of the shipped CMD_BLIT / CMD_VDC_REG.
CMD_VBSTREAM: Final = 0x03

#: Where the sweeps write. Bitmap offset 0 is displayed memory, chosen over the
#: off-screen area on purpose: 64 KiB machines have shown an unexplained loss
#: rate above 24000 (c64cast issue #361), and a probe should not sit on top of a
#: second open question.
SWEEP_DST: Final = 0x0000
#: The single-bit-error hunt gets its own region so the two do not overwrite
#: each other's evidence between stages.
BLIT_DST: Final = 0x2000
BLIT_BYTES: Final = 4096

#: Pre-filled into the destination before each unpolled stream. A stream that
#: stops early leaves this behind, which is what makes "wrote fewer bytes than
#: asked" distinguishable from "wrote the wrong bytes".
SENTINEL: Final = 0xAA

#: Sizes swept through the blanking window, in bytes. 255 is the ceiling: the
#: stream is indexed by Y from a page-aligned source, so it cannot wrap.
SWEEP_SIZES: Final = (24, 64, 96, 128, 160, 192, 224, 255)

#: MAIL_ARG values for the stream, and the 1 MHz cycle cost per byte each one
#: produces. 0 selects a separate un-padded loop; every other value spins DEX.
PAD_CYCLES: Final = {0: 17, 1: 24, 2: 29, 3: 34}

#: Gap between MAIL_DONE polls. Every DMA read halts the 8502, so polling a
#: running command steals cycles from the command being measured — see the same
#: constant in vdc_c128.py, where a 5 ms gap made blits hang that a 50 ms gap
#: completed.
POLL_INTERVAL: Final = 0.050
VRAM_TYPE_BIT: Final = 0x10  # R28 bit 4: set selects 64 KiB addressing

#: Bits the 8563 returns set on a register read whatever was written to them,
#: from VICE's ``vdc-mem.c`` regmask table. Comparing a raw readback against the
#: value programmed reports R9 = $01 as a mismatch when it reads back $E1.
REG_READ_ONES: Final = {
    5: 0xE0,
    8: 0xFC,
    9: 0xE0,
    10: 0x80,
    11: 0xE0,
    23: 0xE0,
    28: 0x0F,
    29: 0xE0,
    36: 0xF0,
    37: 0x3F,
}


# ---------------------------------------------------------------------------
# The probe cartridge
# ---------------------------------------------------------------------------


def probe_resident_source() -> str:
    """The frozen resident loop. ``blit`` is the shipped one verbatim; ``vbstr``
    is the unpolled blanking stream this probe exists to exercise."""
    table = "\n".join(
        f"        .byte ${reg:02X},${value:02X}"
        for reg, value in sorted(vdc.BITMAP_640x200_REGS.items())
    )
    return f"""
; ---- entry: bank the cartridge out and take over the machine -------------
main:   LDA #${vdc_rom.MMU_ALL_RAM_IO:02X}
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
        LDA #$00
        STA ${vdc_rom.MAIL_CMD:04X}
        STA ${vdc_rom.MAIL_DONE:04X}

; ---- the idle loop -------------------------------------------------------
poll:   INC ${vdc_rom.MAIL_BEAT:04X}
        LDA ${vdc_rom.MAIL_CMD:04X}
        BEQ poll
        CMP #${vdc_rom.CMD_BLIT:02X}
        BEQ c_blit
        CMP #${CMD_VBSTREAM:02X}
        BEQ c_vbs
        JMP ack             ; unknown command: acknowledge, do nothing
c_blit: JSR blit
        JMP ack
c_vbs:  JSR vbstr
ack:    LDA #$00
        STA ${vdc_rom.MAIL_CMD:04X}
        INC ${vdc_rom.MAIL_DONE:04X}
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

; ---- blit: the shipped polled path, MAIL_CNT bytes ----------------------
blit:   LDX #${vdc.R.UPDATE_HI:02X}
        LDA ${vdc_rom.MAIL_DST_HI:04X}
        JSR vdcw
        LDX #${vdc.R.UPDATE_LO:02X}
        LDA ${vdc_rom.MAIL_DST_LO:04X}
        JSR vdcw
        LDA #$1F
        STA $D600           ; select R31 once and stream through it
        LDA ${vdc_rom.MAIL_SRC_LO:04X}
        STA $FB
        LDA ${vdc_rom.MAIL_SRC_HI:04X}
        STA $FC
        LDA ${vdc_rom.MAIL_CNT_LO:04X}
        STA $FD
        LDA ${vdc_rom.MAIL_CNT_HI:04X}
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

; ---- vbstr: MAIL_CNT_LO bytes into blanking, with NO ready poll ----------
; MAIL_ARG picks the spacing: 0 runs the tight loop, N > 0 spins DEX N times
; per byte. The whole question is which spacings survive and how far.
vbstr:  LDX #${vdc.R.UPDATE_HI:02X}
        LDA ${vdc_rom.MAIL_DST_HI:04X}
        JSR vdcw
        LDX #${vdc.R.UPDATE_LO:02X}
        LDA ${vdc_rom.MAIL_DST_LO:04X}
        JSR vdcw
        LDA ${vdc_rom.MAIL_SRC_LO:04X}
        STA $FB
        LDA ${vdc_rom.MAIL_SRC_HI:04X}
        STA $FC
        LDA ${vdc_rom.MAIL_CNT_LO:04X}
        STA $F9
        LDA ${vdc_rom.MAIL_ARG:04X}
        STA $FA
        LDA #$1F
        STA $D600
        LDY #$00
vw1:    LDA $D600
        AND #${vdc.STATUS_VBLANK:02X}
        BNE vw1             ; leave blanking first, so the window is a whole one
vw2:    LDA $D600
        AND #${vdc.STATUS_VBLANK:02X}
        BEQ vw2             ; blanking has just begun
vw3:    BIT $D600
        BPL vw3             ; the one ready-wait the stream is allowed
        LDA $FA
        BEQ vfast
vslow:  LDA ($FB),Y
        STA $D601
        LDX $FA
vsd:    DEX
        BNE vsd
        INY
        CPY $F9
        BNE vslow
        RTS
vfast:  LDA ($FB),Y
        STA $D601
        INY
        CPY $F9
        BNE vfast
        RTS

vdctab:
{table}
        .byte $FF
"""


def build_probe_crt() -> tuple[bytes, int]:
    """The probe ``.crt`` and the resident image's size in bytes."""
    image = assemble(probe_resident_source(), vdc_rom.RESIDENT_ADDR)
    if len(image) > vdc_rom.RESIDENT_MAX:
        raise ValueError(
            f"resident probe is {len(image)} B; the stub copies {vdc_rom.RESIDENT_MAX}"
        )
    resident = image.ljust(vdc_rom.RESIDENT_MAX, b"\x00")
    rom = assemble(vdc_rom.cartridge_source(resident), vdc_rom.CART_ADDR).ljust(0x2000, b"\x00")
    return vdc.build_c128_crt(rom, name="c64cast VDC probe"), len(image)


# ---------------------------------------------------------------------------
# Link plumbing
# ---------------------------------------------------------------------------


def connect(*, tcp: str | None, serial: str | None) -> TRClient:
    if tcp:
        tx = TcpTransport(tcp, DEFAULT_TCP_PORT)
    else:
        port = serial or autodetect_serial_port()
        if not port:
            raise SystemExit(
                "no TeensyROM+ serial port found. Pass --serial PORT (macOS: "
                "/dev/cu.usbmodem*, Linux: /dev/ttyACM*, Windows: COM3) or --tcp HOST."
            )
        print(f"    serial port: {port}")
        tx = SerialTransport(port, DEFAULT_BAUD)
    client = TRClient(tx)
    client.connect()
    return client


def make_porthole(client: TRClient) -> vdc.VdcPorthole:
    def write(addr: int, data: bytes) -> None:
        client.write_segment(addr, data)

    def read(addr: int, n: int) -> bytes | None:
        try:
            return client.read_segment(addr, n)
        except (OSError, TRError):
            return None

    return vdc.VdcPorthole(write, read)


class Probe:
    """The run: link, cartridge, and the hang tally that outlives a relaunch."""

    def __init__(self, client: TRClient, settle: float) -> None:
        self.client = client
        self.settle = settle
        self.port = make_porthole(client)
        self.hangs = 0
        self.commands = 0
        #: Buffers the host has put in C128 RAM. A relaunch resets the machine
        #: and takes them with it, so a sweep that survives a hang would go on
        #: streaming whatever the reset left behind unless they are restored.
        self.staged: dict[int, bytes] = {}
        #: R28 bit 4 as the KERNAL left it, sampled before launch.
        self.vram_64k: bool | None = None

    def stage(self, addr: int, data: bytes) -> None:
        self.staged[addr] = data
        self.client.write_segment(addr, data)

    # ---- cartridge --------------------------------------------------------

    def launch(self) -> bool:
        crt, used = build_probe_crt()
        print(f"    .crt {len(crt)} B   resident probe {used} B of {vdc_rom.RESIDENT_MAX}")
        self.client.reset()
        time.sleep(self.settle)
        # R28 bit 4 is the documented RAM-type flag, but the cartridge programs
        # R28 for itself at boot, so it has to be sampled here while the
        # KERNAL's value still stands.
        with contextlib.suppress(OSError, TRError):
            r28 = self.port.read_reg(vdc.R.CHARSET_ADDR)
            if r28 is not None:
                self.vram_64k = bool(r28 & VRAM_TYPE_BIT)
        self.client._drain_stale(0.4)
        with contextlib.suppress(OSError, TRError):
            self.client.delete_file(UPLOAD_PATH)
        self.client.post_file(crt, UPLOAD_PATH)
        self.client.launch_file(UPLOAD_PATH)
        # LaunchFile acks and then streams console text; draining to silence is
        # the only safe way to know the link is ours again.
        self.client.drain_after_command(0.6)
        time.sleep(self.settle)
        for _ in range(12):
            try:
                a = self.client.read_segment(vdc_rom.MAIL_BEAT, 1)
                time.sleep(0.2)
                b = self.client.read_segment(vdc_rom.MAIL_BEAT, 1)
            except (OSError, TRError):
                time.sleep(0.5)
                continue
            if a != b:
                print(f"    heartbeat moving: ${a[0]:02X} -> ${b[0]:02X}   ALIVE")
                for addr, data in self.staged.items():
                    self.client.write_segment(addr, data)
                return True
            time.sleep(0.4)
        return False

    # ---- commands ---------------------------------------------------------

    def issue(self, cmd: int, *, arg: int = 0, dst: int = 0, count: int = 0, src: int = 0) -> bool:
        """Run one command. False means it never acknowledged, which is the
        intermittent hang (c64cast issue #356); the cartridge is relaunched so
        the sweep can carry on, and the tally is reported at the end."""
        self.commands += 1
        try:
            before = self.client.read_segment(vdc_rom.MAIL_DONE, 1)[0]
            self.client.write_segment(
                vdc_rom.MAIL_ARG,
                bytes([arg, dst & 0xFF, dst >> 8, count & 0xFF, count >> 8, src & 0xFF, src >> 8]),
            )
            self.client.write_segment(vdc_rom.MAIL_CMD, bytes([cmd]))
            t0 = time.perf_counter()
            while time.perf_counter() - t0 < 20.0:
                if self.client.read_segment(vdc_rom.MAIL_DONE, 1)[0] != before:
                    return True
                time.sleep(POLL_INTERVAL)
        except (OSError, TRError):
            pass
        self.hangs += 1
        print("        (no acknowledgment - relaunching the cartridge)")
        self.launch()
        return False

    def read_stable(self, addr: int, n: int) -> tuple[bytes | None, bool]:
        """Read VRAM until two passes agree.

        A single porthole read burst comes back with a corrupted byte about one
        pass in twenty, so a lone readback cannot tell a VRAM error from a read
        error — and this whole tool is a hunt for VRAM errors."""
        seen: list[bytes] = []
        for _ in range(4):
            got = self.port.read_ram(addr, n)
            if got is None:
                return None, False
            if got in seen:
                return got, True
            seen.append(got)
        return seen[-1], False


# ---------------------------------------------------------------------------
# Difference reporting
# ---------------------------------------------------------------------------


def describe(payload: bytes, got: bytes, sentinel: int) -> str:
    """How ``got`` differs from ``payload``, in the terms the findings are in."""
    n = len(payload)
    first = next((i for i in range(n) if got[i] != payload[i]), None)
    if first is None:
        return f"clean ({n}/{n})"
    for k in range(1, 17):
        if got[first : n - k] == payload[first + k : n] and set(got[n - k :]) <= {sentinel}:
            return f"{first} correct, then SHIFTED by {k} (tail still ${sentinel:02X})"
    if set(got[first:]) <= {sentinel}:
        return f"{first} correct, then untouched (${sentinel:02X})"
    bad = [i for i in range(n) if got[i] != payload[i]]
    xors = collections.Counter(got[i] ^ payload[i] for i in bad)
    top = " ".join(f"${v:02X}x{c}" for v, c in xors.most_common(3))
    return f"{first} correct, {len(bad)} wrong, xor {top}"


# ---------------------------------------------------------------------------
# Stages
# ---------------------------------------------------------------------------


def vram_is_16k(p: Probe) -> bool:
    """Is this a 16 KiB VDC? The C128 Editor ROM's own test.

    Force 64 KiB addressing, clear $0000, write $FF at $8000, read $0000 back.
    R28 bit 4 cannot answer this by itself: it configures the addressing rather
    than reporting the chips, so the test has to assume 64 KiB and see whether
    the far write aliases home. Any nonzero byte counts, because a 4416 machine
    is four bits wide and need not alias the whole byte."""
    r28 = p.port.read_reg(vdc.R.CHARSET_ADDR)
    if r28 is not None:
        p.port.write_reg(vdc.R.CHARSET_ADDR, (r28 | VRAM_TYPE_BIT) & ~REG_READ_ONES[28])
    p.port.write_ram(0x0000, b"\x00")
    p.port.write_ram(0x8000, b"\xff")
    got = p.port.read_ram(0x0000, 1)
    return bool(got and got[0])


def _hexb(v: int | None) -> str:
    return "--" if v is None else f"${v:02X}"


def writes_land(p: Probe) -> bool:
    """Bounce a pattern off R18/R19 to prove a host register write reaches the chip.

    R18/R19 rather than a scratch register because they are read/write on every
    revision and hold the update address, which every later operation sets for
    itself anyway. Two patterns rather than one because a single value could
    match whatever the pointer already held."""
    for hi, lo in ((0x5A, 0xA5), (0xA5, 0x5A)):
        p.port.write_reg(vdc.R.UPDATE_HI, hi)
        p.port.write_reg(vdc.R.UPDATE_LO, lo)
        got = (p.port.read_reg(vdc.R.UPDATE_HI), p.port.read_reg(vdc.R.UPDATE_LO))
        print(
            f"    R18/R19 loopback          wrote ${hi:02X}/${lo:02X}, "
            f"read {_hexb(got[0])}/{_hexb(got[1])}"
        )
        if got != (hi, lo):
            return False
    return True


def stage_identity(p: Probe) -> bool:
    """Identify the chip, and decide whether the rest of the run can mean anything.

    Returns False when host register writes are not reaching the VDC."""
    print("\n[2] identity")
    status = p.port.read_status()
    if status is None:
        print("    $D600 unreadable")
        return False
    version = vdc.VDC_VERSIONS.get(status & vdc.STATUS_VERSION_MASK, "unknown")
    print(f"    $D600 status              ${status:02X}")
    print(f"    chip revision             {version}  (bits 0-2 = {status & 7})")
    r28 = p.port.read_reg(vdc.R.CHARSET_ADDR)
    if r28 is not None:
        size = "64 KiB" if r28 & 0x10 else "16 KiB"
        print(f"    R28 = ${r28:02X}               VRAM {size}")
    for reg in (vdc.R.H_TOTAL, vdc.R.V_TOTAL, vdc.R.V_DISPLAYED, vdc.R.CHAR_V_TOTAL):
        want = vdc.BITMAP_640x200_REGS[reg] | REG_READ_ONES.get(reg, 0)
        got = p.port.read_reg(reg)
        if got is None:
            print(f"    R{reg:<2d} unreadable")
            continue
        mark = "OK" if got == want else f"MISMATCH (wanted ${want:02X})"
        print(f"    R{reg:<2d} = ${got:02X} ({got:3d})           {mark}")
    if writes_land(p):
        boot = "unread" if p.vram_64k is None else "64 KiB" if p.vram_64k else "16 KiB"
        aliases = vram_is_16k(p)
        print(f"    VRAM as KERNAL set R28    {boot}  (configured, not measured)")
        print(f"    VRAM by aliasing at $8000 {'16 KiB' if aliases else '64 KiB'}")
        if aliases or p.vram_64k is False:
            r28 = p.port.read_reg(vdc.R.CHARSET_ADDR)
            if r28 is not None:
                p.port.write_reg(vdc.R.CHARSET_ADDR, r28 & ~VRAM_TYPE_BIT & ~REG_READ_ONES[28])
            print("    -> cleared R28 bit 4. The cartridge is frozen and programs")
            print("       64 KiB addressing on every machine, which decodes wrong")
            print("       here. Timing registers are untouched, so stage 3 and the")
            print("       stream window stay comparable; only the picture differs.")
        return True
    print("\n    !! host register writes are not reaching the VDC.")
    print("       Selecting a register means writing $D600 first, so every readback")
    print("       in stages 4 and 5 would describe some fixed byte pattern rather")
    print("       than what the blit wrote. They are skipped instead of reported.")
    print("       Stage 3 below needs no register write and is still measured.")


def stage_blanking(p: Probe, samples: int) -> float:
    """Fraction of the frame with $D600 bit 5 set.

    Sampled from the host rather than the 8502: each read is a serial round trip
    with its own jitter, so the samples land at uncorrelated points in the frame
    without needing a timer the C128 does not have free."""
    print(f"\n[3] blanking duty cycle ($D600 bit 5, {samples} samples)")
    hits = 0
    taken = 0
    for _ in range(samples):
        got = p.port.read_status()
        if got is None:
            continue
        taken += 1
        hits += bool(got & vdc.STATUS_VBLANK)
    if not taken:
        print("    no samples")
        return 0.0
    pct = 100.0 * hits / taken
    v_total = vdc.BITMAP_640x200_REGS[vdc.R.V_TOTAL] + 1
    per_row = vdc.BITMAP_640x200_REGS[vdc.R.CHAR_V_TOTAL] + 1
    lines = v_total * per_row
    blanked = lines - vdc.BITMAP_H
    print(f"    bit 5 set in {pct:.1f}% of samples")
    print(f"    register program: {lines} scanlines, {vdc.BITMAP_H} displayed, {blanked} blanked")
    print(f"    -> predicted {100.0 * blanked / lines:.1f}% if bit 5 marks the whole blank,")
    print("       ~1.5% if it marks only the vsync pulse")
    return pct


def stage_blit_errors(p: Probe, rounds: int) -> list[int]:
    """The single-bit-error hunt. Three payloads, because a bit that only ever
    clears is a different fault from one that only ever sets, and a ramp cannot
    tell them apart."""
    print(f"\n[4] polled-blit integrity ({BLIT_BYTES} B x {rounds} rounds x 3 payloads)")
    payloads = {
        "ramp": bytes((i * 37 + 11) & 0xFF for i in range(BLIT_BYTES)),
        "$00 ": bytes(BLIT_BYTES),
        "$FF ": b"\xff" * BLIT_BYTES,
    }
    all_xors: list[int] = []
    for name, payload in payloads.items():
        for r in range(rounds):
            p.client.write_segment(vdc_rom.FRAMEBUF_ADDR, payload)
            if not p.issue(
                vdc_rom.CMD_BLIT, dst=BLIT_DST, count=BLIT_BYTES, src=vdc_rom.FRAMEBUF_ADDR
            ):
                continue
            got, stable = p.read_stable(BLIT_DST, BLIT_BYTES)
            if got is None:
                print(f"    {name} {r + 1}: VRAM unreadable")
                continue
            bad = [i for i in range(BLIT_BYTES) if got[i] != payload[i]]
            flag = "" if stable else "  (readback never settled - treat as noisy)"
            if not bad:
                print(f"    {name} {r + 1}: clean{flag}")
                continue
            xors = collections.Counter(got[i] ^ payload[i] for i in bad)
            all_xors.extend(got[i] ^ payload[i] for i in bad)
            cleared = sum(1 for i in bad if payload[i] & ~got[i])
            top = " ".join(f"${v:02X}x{c}" for v, c in xors.most_common(4))
            print(
                f"    {name} {r + 1}: {len(bad)} wrong  xor {top}  "
                f"({cleared} bits cleared, {len(bad) - cleared} set)  first at {bad[0]}{flag}"
            )
    return all_xors


def stage_vblank_stream(p: Probe, pads: tuple[int, ...]) -> dict[int, list[tuple[int, int]]]:
    """Stream unpolled inside blanking, sweeping size against spacing. Returns
    per spacing the (size asked, bytes correct) pairs, which is where the window
    falls out."""
    print("\n[5] unpolled stream inside blanking")
    print("    each cell: bytes correct before the first divergence, and how it failed")
    payload = bytes((i * 37 + 11) & 0xFF for i in range(256))
    sentinel = bytes([SENTINEL]) * 256
    p.stage(vdc_rom.FRAMEBUF_ADDR, payload)
    p.stage(vdc_rom.FRAMEBUF_ADDR + 0x100, sentinel)

    windows: dict[int, list[tuple[int, int]]] = {}
    for pad in pads:
        cycles = PAD_CYCLES[pad]
        print(f"\n    --- {cycles} cycles/byte (MAIL_ARG={pad}) ---")
        for n in SWEEP_SIZES:
            if not p.issue(
                vdc_rom.CMD_BLIT, dst=SWEEP_DST, count=256, src=vdc_rom.FRAMEBUF_ADDR + 0x100
            ):
                continue
            if not p.issue(
                CMD_VBSTREAM, arg=pad, dst=SWEEP_DST, count=n, src=vdc_rom.FRAMEBUF_ADDR
            ):
                continue
            got, stable = p.read_stable(SWEEP_DST, n)
            if got is None:
                print(f"      {n:3d} B: VRAM unreadable")
                continue
            note = describe(payload[:n], got, SENTINEL)
            flag = "" if stable else "  (noisy readback)"
            print(f"      {n:3d} B: {note}{flag}")
            first = next((i for i in range(n) if got[i] != payload[i]), n)
            windows.setdefault(cycles, []).append((n, first))
    return windows


def summarize(
    p: Probe,
    duty: float,
    xors: list[int],
    windows: dict[int, list[tuple[int, int]]],
    version: str,
    measured: bool,
) -> None:
    print("\n" + "=" * 72)
    print("SUMMARY - paste this whole transcript back")
    print("=" * 72)
    print(f"  chip revision            {version}")
    print(f"  blanking duty cycle      {duty:.1f}%  (this rig measured 22-26%)")
    if not measured:
        print("  blit + stream            NOT MEASURED - host register writes")
        print("                           never reached the VDC (see stage 2)")
        print("=" * 72)
        return

    if not xors:
        counts = "none seen"
    else:
        top = collections.Counter(xors).most_common(3)
        counts = " ".join(f"${v:02X}x{c}" for v, c in top)
    single = sum(1 for v in xors if bin(v).count("1") == 1)
    print(f"  blit error xors          {counts}")
    print(f"  of {len(xors)} errors, {single} were a single bit (this rig: all of them, all $40)")

    for cycles in sorted(windows):
        # Only a run that actually diverged locates the window. Keeping the
        # clean ones, which report their own size, makes a machine with no
        # errors at all report the median of SWEEP_SIZES as its window.
        capped = [first for size, first in windows[cycles] if first < size]
        if capped:
            median = int(statistics.median(capped))
            print(
                f"  {cycles:2d} cycles/byte          window ~{median} bytes "
                f"= {median * cycles / 1e3:.2f} ms of blanking"
            )
        else:
            print(f"  {cycles:2d} cycles/byte          no divergence at any size tested")
    print(f"  hangs                    {p.hangs} of {p.commands} commands (this rig: ~1 in 6)")
    print("=" * 72)


def blank_screen(port: vdc.VdcPorthole) -> None:
    """Leave the 80-column display solid black. A reset drops the machine into
    the TeensyROM menu, which is 40 columns, so whatever this run left in VDC RAM
    would otherwise stay on the RGBI output."""
    port.write_reg(vdc.R.H_SCROLL_CTRL, vdc.H_SCROLL_BITMAP_BIT | vdc.H_SCROLL_NEUTRAL)
    port.write_reg(vdc.R.FG_BG_COLOR, 0x00)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--tcp", metavar="HOST", help="TeensyROM+ over TCP")
    ap.add_argument("--serial", metavar="PORT", help="TeensyROM+ over serial (default: autodetect)")
    ap.add_argument("--samples", type=int, default=500, help="blanking duty-cycle samples")
    ap.add_argument("--rounds", type=int, default=2, help="blit rounds per payload in stage 4")
    ap.add_argument(
        "--pads",
        default="0,1,2,3",
        help="MAIL_ARG spacings for stage 5 (0=17 cyc/B, 1=24, 2=29, 3=34)",
    )
    ap.add_argument("--reset-settle", type=float, default=3.0)
    ap.add_argument("--no-reset-exit", action="store_true", help="leave the cartridge running")
    args = ap.parse_args()

    try:
        pads = tuple(int(x) for x in args.pads.split(","))
    except ValueError:
        ap.error("--pads takes a comma-separated list of integers")
    if any(pad not in PAD_CYCLES for pad in pads):
        ap.error(f"--pads values must be among {sorted(PAD_CYCLES)}")

    print("[1] build + upload + launch the probe cartridge")
    client = connect(tcp=args.tcp, serial=args.serial)
    p = Probe(client, args.reset_settle)
    try:
        if not p.launch():
            print("    heartbeat NOT moving - the cartridge did not boot.")
            print("    Check that the TR+ is in a C128 and that the .crt launched in 128 mode.")
            return 1
        status = p.port.read_status()
        version = (
            vdc.VDC_VERSIONS.get(status & vdc.STATUS_VERSION_MASK, "unknown")
            if status is not None
            else "unreadable"
        )
        measured = stage_identity(p)
        duty = stage_blanking(p, args.samples)
        xors: list[int] = []
        windows: dict[int, list[tuple[int, int]]] = {}
        if measured:
            xors = stage_blit_errors(p, args.rounds)
            windows = stage_vblank_stream(p, pads)
        summarize(p, duty, xors, windows, version, measured)
    except (OSError, TRError, TimeoutError) as e:
        print(f"\nABORTED: {e}")
        return 1
    finally:
        if not args.no_reset_exit:
            with contextlib.suppress(OSError, TRError):
                blank_screen(p.port)
            print("\nresetting the C128 ...")
            with contextlib.suppress(OSError, TRError):
                client.reset()
        client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
