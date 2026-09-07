"""C128 VDC (8563/8568) support primitives — hardware-independent core.

This module is the reusable foundation for driving a Commodore 128's VDC (the
80-column RGBI video chip) as a c64cast display target on the **TeensyROM+
backend only** (the VDC is a C128-exclusive part, and the TR+ is the only
backend that plugs into a C128). It is deliberately transport-agnostic: every
function here takes plain callables or numpy arrays, so it is exercised
entirely offline by ``tests/test_vdc.py`` and reused by both the hardware probe
(``scripts/diags/vdc_probe.py``) and — eventually — a ``VDCDisplayMode`` family
and the backend's ``write_vdc_region``.

Nothing in this module is wired into a backend or the display-mode hierarchy
yet. It is pre-implementation tooling; see ``~/src/c128-vdc-plan.md`` (outside
the repo) for the full plan and the open hardware questions.

## Why the VDC is hard

The VDC's 16 KiB (later 64 KiB) video RAM is **not** in the CPU / cartridge-DMA
address space. The only access is a two-register porthole:

* ``$D600`` — address/status register: write a register number to select it;
  read it for status (bit 7 = ready, bits 0-2 = chip version).
* ``$D601`` — data register for the currently selected VDC register.

To touch VDC RAM you set R18/R19 (the "update address"), then read or write R31
repeatedly — R31 auto-increments R18/R19. Bulk fills and VRAM-to-VRAM copies
run in VDC hardware via R24/R30/R32/R33 and are the only *fast* way to move
data around inside VRAM. Getting host data *into* VRAM is always the slow
per-byte porthole path.

## The RGBI palette

The VDC emits 16 fixed RGBI colors in the standard CGA/EGA index order, so the
VDC palette index *is* the RGBI nibble — there is no per-unit calibration knob
like the VIC's ``host_palette``. Values below are the de-facto reference set
(matches ``~/src/vdc-ega/palettes/vdc-irfanview.pal``); note the CGA "brown"
special case at index 6 (``170,85,0`` rather than ``170,170,0``).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Final

import numpy as np

# ---------------------------------------------------------------------------
# The porthole registers (in the C128's C64/128 I/O space)
# ---------------------------------------------------------------------------

D600_ADDR_STATUS: Final = 0xD600  # write: select register; read: status
D601_DATA: Final = 0xD601  # data for the selected register

STATUS_READY: Final = 0x80  # bit 7 of $D600 — set when a register access completed
STATUS_VBLANK: Final = 0x20  # bit 5 — in vertical blanking (safe to flip R12/R13)
STATUS_VERSION_MASK: Final = 0x07  # bits 0-2 — chip revision (see VDC_VERSIONS)

VDC_VERSIONS: Final = {
    0: "8563 R7A",
    1: "8563 R8/R9",
    2: "8568",
}


# ---------------------------------------------------------------------------
# VDC internal registers (the ones c64cast needs; there are 37 total)
# ---------------------------------------------------------------------------


class R:
    """VDC register numbers. Selected by writing the number to ``$D600``, then
    read/written through ``$D601``."""

    H_TOTAL: Final = 0
    H_DISPLAYED: Final = 1
    H_SYNC_POS: Final = 2
    SYNC_WIDTH: Final = 3
    V_TOTAL: Final = 4
    V_TOTAL_ADJUST: Final = 5
    V_DISPLAYED: Final = 6
    V_SYNC_POS: Final = 7
    INTERLACE: Final = 8
    CHAR_V_TOTAL: Final = 9  # (scanlines per char row) - 1; =1 gives 8x2 attr blocks
    CURSOR_START: Final = 10
    CURSOR_END: Final = 11
    DISPLAY_HI: Final = 12  # screen/bitmap RAM start, high byte
    DISPLAY_LO: Final = 13
    CURSOR_HI: Final = 14
    CURSOR_LO: Final = 15
    LIGHTPEN_HI: Final = 16  # read-only
    LIGHTPEN_LO: Final = 17
    UPDATE_HI: Final = 18  # VRAM r/w address for R31, high byte
    UPDATE_LO: Final = 19
    ATTR_HI: Final = 20  # attribute RAM start, high byte
    ATTR_LO: Final = 21
    CHAR_H_TOTAL: Final = 22
    CHAR_V_DISPLAYED: Final = 23
    V_SCROLL_CTRL: Final = 24  # bit 7: 0 = block WRITE (fill), 1 = block COPY
    H_SCROLL_CTRL: Final = 25  # bit 7: bitmap mode on; bit 6: attribute enable
    FG_BG_COLOR: Final = 26  # fg/bg when attributes are disabled
    ROW_ADDR_INCREMENT: Final = 27
    CHARSET_ADDR: Final = 28  # char-set base; also |= 0x18 selects 64 KiB VRAM
    UNDERLINE_SCAN: Final = 29
    WORD_COUNT: Final = 30  # writing this triggers a block fill/copy of N bytes
    DATA: Final = 31  # VRAM data; reads/writes auto-increment R18/R19
    BLOCK_COPY_SRC_HI: Final = 32
    BLOCK_COPY_SRC_LO: Final = 33
    DISPLAY_ENABLE_BEGIN: Final = 34
    DISPLAY_ENABLE_END: Final = 35
    DRAM_REFRESH: Final = 36  # 0 = minimum refresh -> fastest RAM access


V_SCROLL_COPY_BIT: Final = 0x80  # R24 bit 7: set = block copy, clear = block fill
H_SCROLL_BITMAP_BIT: Final = 0x80  # R25 bit 7: bitmap (graphics) mode
H_SCROLL_ATTR_BIT: Final = 0x40  # R25 bit 6: per-cell attributes enabled
CHARSET_64K_BITS: Final = 0x18  # R28: OR in to address the full 64 KiB


# ---------------------------------------------------------------------------
# The RGBI palette (index == RGBI nibble, standard CGA/EGA order)
# ---------------------------------------------------------------------------

# (R, G, B), 0-255. Index 0..15. Matches ~/src/vdc-ega/palettes/vdc-irfanview.pal.
VDC_PALETTE_RGB: Final = (
    (0x00, 0x00, 0x00),  # 0  black
    (0x55, 0x55, 0x55),  # 1  dark grey
    (0x00, 0x00, 0xAA),  # 2  blue
    (0x55, 0x55, 0xFF),  # 3  light blue
    (0x00, 0xAA, 0x00),  # 4  green
    (0x55, 0xFF, 0x55),  # 5  light green
    (0x00, 0xAA, 0xAA),  # 6  cyan
    (0x55, 0xFF, 0xFF),  # 7  light cyan
    (0xAA, 0x00, 0x00),  # 8  red
    (0xFF, 0x55, 0x55),  # 9  light red
    (0xAA, 0x00, 0xAA),  # 10 magenta
    (0xFF, 0x55, 0xFF),  # 11 light magenta
    (0xAA, 0x55, 0x00),  # 12 brown (CGA special case, not 0xAAAA00)
    (0xFF, 0xFF, 0x55),  # 13 yellow
    (0xAA, 0xAA, 0xAA),  # 14 light grey
    (0xFF, 0xFF, 0xFF),  # 15 white
)

#: (16, 3) float32 in **RGB** order, 0-255.
VDC_PALETTE = np.array(VDC_PALETTE_RGB, dtype=np.float32)
#: (16, 3) float32 in **BGR** order — matches ``palette.C64_PALETTE_BGR`` so the
#: existing quantizer helpers can be pointed at a VDC target.
VDC_PALETTE_BGR = VDC_PALETTE[:, ::-1].copy()


# ---------------------------------------------------------------------------
# Bitmap-mode geometry
# ---------------------------------------------------------------------------

BITMAP_W: Final = 640
BITMAP_H: Final = 200
BITMAP_BYTES: Final = BITMAP_W * BITMAP_H // 8  # 16000
ATTR_COLS: Final = BITMAP_W // 8  # 80
ATTR_ROWS: Final = BITMAP_H // 2  # 100  (8x2 colour blocks)
ATTR_BYTES: Final = ATTR_COLS * ATTR_ROWS  # 8000
FRAME_BYTES: Final = BITMAP_BYTES + ATTR_BYTES  # 24000

# Default VRAM layout for a single 640x200 8x2-colour bitmap frame (from
# ~/src/vdc-ega/src/view320x200x4.bas): bitmap at 0, attributes at 16000.
BITMAP_BASE: Final = 0x0000
ATTR_BASE: Final = 16000

# The 640x200 / 8x2-block / 64 KiB register program, straight from that .bas.
# Applied *after* selecting 64 KiB VRAM (R28 |= CHARSET_64K_BITS) and bitmap
# mode (R25 |= H_SCROLL_BITMAP_BIT).
# The full register program, not a set of deltas: entered from C64 mode the VDC's
# registers are unprogrammed (they read back $FF), so there is no working base
# timing to inherit. vdc-ega's R0=127/R4=155 is 312 scanlines (~50 Hz) and rolls
# on an RGBI monitor expecting the C128's native ~60 Hz, so the timing here is
# 128 char clocks x 264 scanlines instead — verified stable on an 8563 R8/R9.
BITMAP_640x200_REGS: Final = {
    R.H_TOTAL: 126,
    R.H_DISPLAYED: 80,
    R.H_SYNC_POS: 102,
    R.SYNC_WIDTH: 0x49,
    R.V_TOTAL: 131,
    R.V_TOTAL_ADJUST: 0,
    R.V_DISPLAYED: 100,  # 100 rows x 2 scanlines = 200 displayed lines
    R.V_SYNC_POS: 116,
    R.INTERLACE: 0,
    R.CHAR_V_TOTAL: 1,  # 2 scanlines per char row -> 8x2 attribute blocks
    R.CURSOR_START: 0x20,
    R.CURSOR_END: 7,
    R.DISPLAY_HI: BITMAP_BASE >> 8,
    R.DISPLAY_LO: BITMAP_BASE & 0xFF,
    R.CURSOR_HI: 0,
    R.CURSOR_LO: 0,
    R.ATTR_HI: ATTR_BASE >> 8,
    R.ATTR_LO: ATTR_BASE & 0xFF,
    R.CHAR_H_TOTAL: 0x78,
    R.CHAR_V_DISPLAYED: 8,
    R.V_SCROLL_CTRL: 0x20,
    R.H_SCROLL_CTRL: H_SCROLL_BITMAP_BIT | H_SCROLL_ATTR_BIT,
    R.FG_BG_COLOR: 0xF0,
    R.ROW_ADDR_INCREMENT: 0,
    R.CHARSET_ADDR: CHARSET_64K_BITS,
    R.UNDERLINE_SCAN: 7,
    R.DISPLAY_ENABLE_BEGIN: 0x7D,
    R.DISPLAY_ENABLE_END: 0x64,
    R.DRAM_REFRESH: 0,  # minimum refresh -> fastest porthole RAM access
}


# ---------------------------------------------------------------------------
# The porthole: read/write VDC registers and RAM through $D600/$D601
# ---------------------------------------------------------------------------

WriteFn = Callable[[int, bytes], None]
ReadFn = Callable[[int, int], bytes | None]


class VdcPorthole:
    """Drive the VDC through its ``$D600``/``$D601`` porthole, given a backend's
    memory write and read primitives.

    ``write(addr, data)`` writes ``data`` to consecutive C64/128 addresses
    starting at ``addr`` (c64cast's ``backend.write_memory`` / the TR
    ``WriteC64Mem`` token). ``read(addr, n)`` reads ``n`` bytes back (``None``
    on failure), mirroring ``backend.read_memory``.

    Every RAM access here is the slow per-byte porthole path — this class is for
    the probe and for small transfers, not a frame pump. The frame pump is an
    on-C128 8502 routine (or an ARM-side firmware token); see the plan doc.
    """

    def __init__(self, write: WriteFn, read: ReadFn) -> None:
        self._write = write
        self._read = read

    # ---- register access ------------------------------------------------

    def write_reg(self, reg: int, value: int) -> None:
        self._write(D600_ADDR_STATUS, bytes([reg & 0xFF]))
        self._write(D601_DATA, bytes([value & 0xFF]))

    def read_reg(self, reg: int) -> int | None:
        self._write(D600_ADDR_STATUS, bytes([reg & 0xFF]))
        got = self._read(D601_DATA, 1)
        return None if not got else got[0]

    def read_status(self) -> int | None:
        got = self._read(D600_ADDR_STATUS, 1)
        return None if not got else got[0]

    def write_regs(self, regs: dict[int, int]) -> None:
        for reg, value in regs.items():
            self.write_reg(reg, value)

    # ---- RAM access ---------------------------------------------------------

    def set_update_addr(self, vram_addr: int) -> None:
        self.write_reg(R.UPDATE_HI, (vram_addr >> 8) & 0xFF)
        self.write_reg(R.UPDATE_LO, vram_addr & 0xFF)

    def read_ram(self, vram_addr: int, length: int) -> bytes | None:
        """Read ``length`` bytes from VRAM. One porthole round-trip per byte.

        Rarely, a long burst comes back with a single corrupted byte (~1 pass in
        20 at 300 bytes). A caller that must trust the bytes should re-read and
        compare rather than chunk the read, which does not help."""
        self.set_update_addr(vram_addr)
        self._write(D600_ADDR_STATUS, bytes([R.DATA]))
        out = bytearray()
        for _ in range(length):
            got = self._read(D601_DATA, 1)
            if got is None:
                return None
            out += got
        return bytes(out)

    def write_ram(self, vram_addr: int, data: bytes) -> None:
        """Write ``data`` to VRAM. One porthole write per byte (``$D600`` stays
        selected on R31, so it is a single ``write_memory`` of one byte to
        ``$D601`` each). Slow by construction — ~24 KiB is seconds."""
        self.set_update_addr(vram_addr)
        self._write(D600_ADDR_STATUS, bytes([R.DATA]))
        for b in data:
            self._write(D601_DATA, bytes([b]))

    def block_fill(self, vram_addr: int, value: int, count: int) -> None:
        """Fill ``count`` VRAM bytes with ``value`` using the VDC's hardware
        block-write (R24 bit 7 clear). The R31 write places the first byte and
        R30 carries the rest.

        Measured on an 8563 R8/R9 over a TeensyROM+ serial link: 16000 bytes in
        24.5 ms (654 KB/s), 120x the byte-at-a-time poke rate."""
        r24 = self.read_reg(R.V_SCROLL_CTRL) or 0
        self.write_reg(R.V_SCROLL_CTRL, r24 & ~V_SCROLL_COPY_BIT)
        self.set_update_addr(vram_addr)
        self.write_reg(R.DATA, value)  # the VDC copies this byte forward
        self._emit_word_count(count - 1)  # first byte already written

    def block_copy(self, src: int, dst: int, count: int) -> None:
        """Copy ``count`` bytes within VRAM using the VDC's hardware block-copy
        (R24 bit 7 set). Near-free — used for front->back buffer copies and
        scrolling."""
        r24 = self.read_reg(R.V_SCROLL_CTRL) or 0
        self.write_reg(R.V_SCROLL_CTRL, r24 | V_SCROLL_COPY_BIT)
        self.write_reg(R.BLOCK_COPY_SRC_HI, (src >> 8) & 0xFF)
        self.write_reg(R.BLOCK_COPY_SRC_LO, src & 0xFF)
        self.set_update_addr(dst)
        self._emit_word_count(count)

    def _emit_word_count(self, count: int) -> None:
        """Run ``count`` block operations by writing R30. A write of K performs
        exactly K operations, so a large count goes out 255 at a time.

        No settle between writes: an 8563 R8/R9 completes 1000 bytes in 1.4 ms
        of pure host time, far inside one porthole round trip, and its ready bit
        never drops for a caller to poll."""
        while count > 0:
            chunk = min(count, 255)
            self.write_reg(R.WORD_COUNT, chunk)
            count -= chunk

    def page_flip(self, display_addr: int, attr_addr: int) -> None:
        """Point the VDC at a different bitmap + attribute buffer (4 register
        writes). For tear-free operation the caller should poll ``read_status``
        for ``STATUS_VBLANK`` first."""
        self.write_reg(R.DISPLAY_HI, (display_addr >> 8) & 0xFF)
        self.write_reg(R.DISPLAY_LO, display_addr & 0xFF)
        self.write_reg(R.ATTR_HI, (attr_addr >> 8) & 0xFF)
        self.write_reg(R.ATTR_LO, attr_addr & 0xFF)


# ---------------------------------------------------------------------------
# Probes (both non-destructive)
# ---------------------------------------------------------------------------


def probe_version(port: VdcPorthole) -> str | None:
    """Chip revision from the status register's low 3 bits, or ``None`` if the
    status read failed. On a machine with no VDC (a plain C64) ``$D600`` is
    open bus and this typically reports ``"8568"`` (``0xFF & 7 == 7`` is not a
    known value) or ``None`` — pair it with :func:`probe_present`."""
    status = port.read_status()
    if status is None:
        return None
    return VDC_VERSIONS.get(status & STATUS_VERSION_MASK)


def probe_present(port: VdcPorthole) -> bool:
    """Is there a real VDC behind the porthole? Writes recognizable values to
    the update-address registers (R18/R19 — invisible; they only stage the next
    VRAM access), reads them back, and restores. On a plain C64 ``$D600`` is
    open bus and the round-trip fails."""
    saved_hi, saved_lo = port.read_reg(R.UPDATE_HI), port.read_reg(R.UPDATE_LO)
    try:
        for hi, lo in ((0x2A, 0x55), (0x15, 0xAA)):
            port.write_reg(R.UPDATE_HI, hi)
            port.write_reg(R.UPDATE_LO, lo)
            if port.read_reg(R.UPDATE_HI) != hi or port.read_reg(R.UPDATE_LO) != lo:
                return False
        return True
    finally:
        if saved_hi is not None:
            port.write_reg(R.UPDATE_HI, saved_hi)
        if saved_lo is not None:
            port.write_reg(R.UPDATE_LO, saved_lo)


def probe_ram_size_kib(port: VdcPorthole) -> int | None:
    """16 or 64 (KiB), or ``None`` if a porthole read failed.

    The ``vdclib.a`` trick: on 16 KiB hardware the upper and lower halves of the
    address space alias the same chips, so writing an inverted value at
    ``$3FFF`` shows up when you read ``$BFFF``. Non-destructive — the original
    byte at ``$3FFF`` is restored."""
    original = port.read_ram(0x3FFF, 1)
    if original is None:
        return None
    probe = original[0] ^ 0xFF
    port.write_ram(0x3FFF, bytes([probe]))
    echo = port.read_ram(0xBFFF, 1)
    port.write_ram(0x3FFF, original)  # restore regardless
    if echo is None:
        return None
    return 16 if echo[0] == probe else 64


# ---------------------------------------------------------------------------
# Offline: pack an indexed image into a VDC 640x200 8x2-colour bitmap frame
# ---------------------------------------------------------------------------


def pack_bitmap_frame(indexed: np.ndarray) -> tuple[bytes, bytes]:
    """Convert a ``(200, 640)`` array of VDC palette indices (0-15) into
    ``(bitmap, attributes)`` — 16000 + 8000 bytes, ready to DMA into VRAM at
    :data:`BITMAP_BASE` / :data:`ATTR_BASE`.

    Each 8x2 block gets the two most-populated colours as (background,
    foreground); every pixel picks whichever of the two it is nearer to in RGB
    space, and the bitmap bit is set for foreground. The attribute byte is
    ``(bg << 4) | fg`` (matches ``~/src/vdc-ega/src/bmp320200x4.bas``).

    Caller owns scaling/quantization: feed a 640-wide frame already reduced to
    VDC palette indices (e.g. via ``palette`` helpers against
    :data:`VDC_PALETTE_BGR`, or a 320-wide source column-doubled)."""
    idx = np.asarray(indexed, dtype=np.uint8)
    if idx.shape != (BITMAP_H, BITMAP_W):
        raise ValueError(f"indexed must be {(BITMAP_H, BITMAP_W)}, got {idx.shape}")

    # (100, 80, 2, 8) — block rows, block cols, the 2 scanlines, 8 px across.
    blocks = idx.reshape(ATTR_ROWS, 2, ATTR_COLS, 8).transpose(0, 2, 1, 3)
    flat = blocks.reshape(ATTR_ROWS, ATTR_COLS, 16)  # 16 px per block

    bitmap = np.zeros((BITMAP_H, ATTR_COLS), dtype=np.uint8)
    attr = np.zeros((ATTR_ROWS, ATTR_COLS), dtype=np.uint8)
    pal = VDC_PALETTE  # (16, 3) RGB

    for br in range(ATTR_ROWS):
        for bc in range(ATTR_COLS):
            px = flat[br, bc]  # (16,)
            counts = np.bincount(px, minlength=16)
            order = np.argsort(counts, kind="stable")[::-1]
            bg = int(order[0])
            fg = int(order[1]) if counts[order[1]] else bg
            attr[br, bc] = (bg << 4) | fg
            if fg == bg:
                continue  # solid block: all bits 0 (background)
            # per-pixel: nearer of {bg, fg} in RGB
            d_bg = np.sum((pal[px] - pal[bg]) ** 2, axis=1)
            d_fg = np.sum((pal[px] - pal[fg]) ** 2, axis=1)
            is_fg = (d_fg < d_bg).reshape(2, 8)
            for row in range(2):
                byte = 0
                for x in range(8):
                    if is_fg[row, x]:
                        byte |= 0x80 >> x
                bitmap[br * 2 + row, bc] = byte

    return bitmap.tobytes(), attr.tobytes()


def simulate_frame(bitmap: bytes, attr: bytes) -> np.ndarray:
    """Render a packed VDC bitmap frame back to a ``(200, 640, 3)`` uint8 RGB
    image — what the RGBI monitor would show. Used by the offline preview tool
    and the tests; no hardware."""
    bm = np.frombuffer(bitmap, dtype=np.uint8).reshape(BITMAP_H, ATTR_COLS)
    at = np.frombuffer(attr, dtype=np.uint8).reshape(ATTR_ROWS, ATTR_COLS)

    bits = np.unpackbits(bm, axis=1).reshape(BITMAP_H, BITMAP_W)  # 0/1 per pixel
    fg = (at & 0x0F).repeat(2, axis=0).repeat(8, axis=1)  # (200, 640)
    bg = (at >> 4).repeat(2, axis=0).repeat(8, axis=1)
    idx = np.where(bits == 1, fg, bg).astype(np.uint8)
    return VDC_PALETTE[idx].astype(np.uint8)


def quantize_to_vdc(rgb: np.ndarray) -> np.ndarray:
    """Nearest-VDC-palette index for every pixel of an ``(H, W, 3)`` uint8 RGB
    image. Plain Euclidean in RGB — good enough for the preview tool; the real
    display mode will route through the perceptual quantizer in ``palette``."""
    px = np.asarray(rgb, dtype=np.float32).reshape(-1, 3)
    d = np.sum((px[:, None, :] - VDC_PALETTE[None, :, :]) ** 2, axis=2)
    return d.argmin(axis=1).astype(np.uint8).reshape(rgb.shape[:2])


# ---------------------------------------------------------------------------
# C128-mode CRT container
# ---------------------------------------------------------------------------

_CRT_HEADER_LEN: Final = 0x40
_CHIP_HEADER_LEN: Final = 0x10


def build_c128_crt(rom: bytes, *, name: str = "c64cast VDC") -> bytes:
    """Wrap a <=8192-byte 8502 ROM image in a C128-mode ``.crt`` container.

    A TeensyROM launches this as an ``rtBinC128`` cartridge — GAME and EXROM
    both deasserted, so the C128 boots **native 128 mode** and autostarts the
    ROM at ``$8000`` (see ``~/src/TeensyROM/Source/Teensy/FileParsers.ino`` and
    the reference ``TRMenuFiles/ROMs/C128_789010.crt.h``). c64cast reaches this
    through the existing ``launch_program`` / ``supports_run_crt`` path.

    This builds only the **container**; the ROM body (autostart signature +
    MMU setup + the resident VDC blit routine) is the caller's, and is not yet
    implemented — see :data:`C128_AUTOSTART_SIGNATURE` and the plan doc."""
    if len(rom) > 0x2000:
        raise ValueError(f"C128 cart ROM is one 8 KiB bank; got {len(rom)} bytes")
    body = rom.ljust(0x2000, b"\x00")

    header = bytearray(b"C128 CARTRIDGE  ")  # 16 bytes, space-padded
    header += _CRT_HEADER_LEN.to_bytes(4, "big")  # header length
    header += (1).to_bytes(1, "big") + (0).to_bytes(1, "big")  # version 1.00
    header += (0).to_bytes(2, "big")  # hardware type: generic
    header += b"\x00"  # EXROM (0)
    header += b"\x00"  # GAME (0)
    header += b"\x00" * 6  # reserved
    header += name.encode("ascii", "replace")[:32].ljust(32, b"\x00")
    assert len(header) == _CRT_HEADER_LEN

    chip = bytearray(b"CHIP")
    chip += (_CHIP_HEADER_LEN + len(body)).to_bytes(4, "big")
    chip += (0).to_bytes(2, "big")  # chip type: ROM
    chip += (0).to_bytes(2, "big")  # bank
    chip += (0).to_bytes(2, "big")  # load address (payload runs at $8000)
    chip += len(body).to_bytes(2, "big")
    assert len(chip) == _CHIP_HEADER_LEN

    return bytes(header) + bytes(chip) + body


#: The 10 bytes a C128 scans for at $8000 to autostart a cartridge in 128 mode,
#: matching ``TRMenuFiles/ROMs/C128_789010.crt.h`` byte-for-byte: cold-start
#: ``JMP``, NMI/warm-start ``JMP``, the cartridge-type byte ``$02``, then plain
#: ASCII ``"CBM"`` (not bit-7-set). ``entry`` defaults to $800A — right after
#: this 10-byte header.
def c128_autostart_signature(entry: int = 0x800A) -> bytes:
    return bytes(
        [
            0x4C,
            entry & 0xFF,
            entry >> 8,  # JMP entry  (cold start)
            0x4C,
            entry & 0xFF,
            entry >> 8,  # JMP entry  (NMI / warm start)
            0x02,  # cartridge type: auto-start
            0x43,
            0x42,
            0x4D,  # "CBM"
        ]
    )
