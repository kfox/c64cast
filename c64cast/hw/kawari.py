"""A VIC-II Kawari over the cartridge bus: what it is, and its palette.

The Kawari is an FPGA replacement for the VIC-II. Its extra registers sit at
$D02F-$D03F behind an unlock sequence, and its configuration — chip model,
capabilities and the 16-color RGB palette its digital output drives among it —
is reachable as an "overlay" over the first 256 bytes of its video memory. The
register map is the project's own: `doc/REGISTERS.md` in randyrossi/vicii-kawari.

A stock VIC-II returns $FF from every one of those registers, so $D03F reading
anything else after the unlock, and then a chip-model byte of 0-3, is what
tells the two apart.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from .uci import MemoryBus

log = logging.getLogger(__name__)

DMA_COUNT = 0xD035  # lo then hi, while both ports are set to DMA
HIRES_MODE = 0xD037
VIDEO_MEM_1_ADDR = 0xD039  # port A address, lo then hi
VIDEO_MEM_1_VAL = 0xD03B
VIDEO_MEM_2_ADDR = 0xD03C  # port B address, lo then hi
VIDEO_MEM_FLAGS = 0xD03F

UNLOCK = b"VIC2"
LOCKED = 0xFF
FLAGS_PORT_A_INC = 0x01
FLAGS_BOTH_PORTS_DMA = 0x0F
FLAGS_OVERLAY = 0x20
# Set, it writes palette and other changes to the EEPROM. Nothing here ever
# sets it.
FLAGS_PERSIST = 0x40
FLAGS_DEACTIVATE = 0x80

OVERLAY_CHIP_MODEL = 0x1F
OVERLAY_PALETTE_RGB = 0x40  # 16 x (R, G, B, unused), 6 bits a channel
OVERLAY_VERSION = 0x83
OVERLAY_CAP_LO = 0x87
OVERLAY_VARIANT = 0x90
VARIANT_BYTES = 16
CAP_CONFIG_RGB = 0x08
CHIP_MODELS = ("6567R8", "6569R3", "6567R56A", "6569R1")
PALETTE_COLORS = 16
PALETTE_CHANNELS = PALETTE_COLORS * 3
CHANNEL_MAX = 63
PALETTE_BYTES = PALETTE_COLORS * 4

DMA_COPY_VMEM = 1
# The first firmware whose VMEM copy can write into the overlay.
OVERLAY_COPY_FIRMWARE = (1, 16)
# Where a shot's table waits in video memory for its copy into the overlay:
# the top of VRAM, clear of $0000-$00FF, which the overlay maps.
PALETTE_SLOT = 0xFF00
# The copy `Kawari.stage_palette` sets up, as 6502 source. The double
# buffer's swap IRQ runs it in the commit that flips to a shot's first frame
# (video/modes_irq.py): that commit can come fields after the frame is armed,
# so a host write cannot be timed to it. Clobbers A.
COPY_STAGED_PALETTE_ASM = f"""
            LDA #${OVERLAY_PALETTE_RGB:02X}
            STA ${VIDEO_MEM_1_ADDR:04X}
            LDA #$00
            STA ${VIDEO_MEM_1_ADDR + 1:04X}
            LDA #${DMA_COPY_VMEM:02X}
            STA ${VIDEO_MEM_1_VAL:04X}
"""

_PALETTE_OFFSETS = tuple(
    OVERLAY_PALETTE_RGB + 4 * color + channel
    for color in range(PALETTE_COLORS)
    for channel in range(3)
)
_SPARE_OFFSETS = tuple(OVERLAY_PALETTE_RGB + 4 * color + 3 for color in range(PALETTE_COLORS))


def to_8bit(channel: int) -> int:
    return round((channel & CHANNEL_MAX) * 255 / CHANNEL_MAX)


def to_6bit(channel: int) -> int:
    return round(channel * CHANNEL_MAX / 255)


@dataclass(frozen=True)
class KawariInfo:
    """What a Kawari said about itself when the run started. `palette_rgb` is
    16 RGB triples scaled to 8 bits a channel; `palette_spare` is the unused
    fourth byte of each color, which a copy of a whole table rewrites."""

    model: str
    version: tuple[int, int]
    variant: str
    cap_lo: int
    palette_rgb: tuple[tuple[int, int, int], ...]
    palette_spare: tuple[int, ...]

    @property
    def configurable_rgb(self) -> bool:
        return bool(self.cap_lo & CAP_CONFIG_RGB)


class Kawari:
    """The run's one way into a Kawari's extra registers.

    Every user shares port A, port B and $D03F, so two of them interleaving
    would corrupt each other's addresses; each operation holds the lock from
    activation to deactivation."""

    def __init__(self, bus: MemoryBus, info: KawariInfo) -> None:
        self._bus = bus
        self.info = info
        self._lock = threading.Lock()

    def write_palette(self, channels: Sequence[int]) -> tuple[int, ...] | None:
        """Write 48 6-bit channel values (R, G, B for colors 0-15) and read
        them back, or None when the Kawari would not activate or a read went
        unanswered. Deactivated again either way."""
        bus = self._bus
        with self._lock:
            if not _activate(bus):
                return None
            try:
                _write_flags(bus, FLAGS_OVERLAY)
                for offset, value in zip(_PALETTE_OFFSETS, channels, strict=True):
                    # The overlay port does not auto-increment on these
                    # registers, so each carries its own address.
                    bus.write_memory(
                        f"{VIDEO_MEM_1_ADDR:04X}", f"{offset:02X}00{value & CHANNEL_MAX:02X}"
                    )
                read = [_overlay_byte(bus, offset) for offset in _PALETTE_OFFSETS]
            finally:
                _deactivate(bus)
        if any(v is None for v in read):
            return None
        return tuple(v & CHANNEL_MAX for v in read if v is not None)

    def stage_palette(self, channels: Sequence[int], stop: Callable[[], bool]) -> bool:
        """Put 48 6-bit channel values in video memory and set up the copy
        that `COPY_STAGED_PALETTE_ASM` starts. False when the Kawari would
        not activate or `stop` cut the staging short; `drop_palette` then owes
        it a deactivate. Left active either way: the copy is the C64's three
        writes, and the registers must hold this setup until it has run."""
        table = bytearray(PALETTE_BYTES)
        table[3::4] = bytes(self.info.palette_spare)
        for i, value in enumerate(channels):
            table[4 * (i // 3) + i % 3] = value & CHANNEL_MAX
        bus = self._bus
        with self._lock:
            if not _activate(bus):
                return False
            _write_flags(bus, FLAGS_PORT_A_INC)
            _write_word(bus, VIDEO_MEM_1_ADDR, PALETTE_SLOT)
            for i, value in enumerate(table):
                # Writing past $D03B would land on port B, so each byte
                # goes to the data register on its own.
                if i % 16 == 0 and stop():
                    return False
                bus.write_memory(f"{VIDEO_MEM_1_VAL:04X}", f"{value:02X}")
            _write_flags(bus, FLAGS_BOTH_PORTS_DMA | FLAGS_OVERLAY)
            _write_word(bus, VIDEO_MEM_2_ADDR, PALETTE_SLOT)
            _write_word(bus, DMA_COUNT, PALETTE_BYTES)
            bus.flush()
        return True

    def check_palette(self, channels: Sequence[int]) -> bool:
        """Whether the palette now holds `channels`; deactivates either way."""
        bus = self._bus
        with self._lock:
            try:
                _write_flags(bus, FLAGS_OVERLAY)
                read = [_overlay_byte(bus, offset) for offset in _PALETTE_OFFSETS]
            finally:
                _deactivate(bus)
        want = [value & CHANNEL_MAX for value in channels]
        return [None if v is None else v & CHANNEL_MAX for v in read] == want

    def drop_palette(self) -> None:
        with self._lock:
            _deactivate(self._bus)


def _write_word(bus: MemoryBus, address: int, value: int) -> None:
    bus.write_memory(f"{address:04X}", f"{value & 0xFF:02X}{value >> 8:02X}")


def _write_flags(bus: MemoryBus, flags: int) -> None:
    bus.write_memory(f"{VIDEO_MEM_FLAGS:04X}", f"{flags:02X}")


def _deactivate(bus: MemoryBus) -> None:
    _write_flags(bus, FLAGS_DEACTIVATE)
    bus.flush()


def _overlay_byte(bus: MemoryBus, offset: int) -> int | None:
    bus.write_memory(f"{VIDEO_MEM_1_ADDR:04X}", f"{offset:02X}00")
    # On an Ultimate the write rides the DMA socket and the read is REST, so
    # the read could otherwise overtake it.
    bus.flush()
    data = bus.read_memory(VIDEO_MEM_1_VAL, 1)
    return data[0] if data else None


def _activate(bus: MemoryBus) -> bool:
    """Make the extra registers answer: True for a Kawari, False for a stock
    VIC-II or no answer.

    The registers keep their state through a C64 reset, so a run can find
    them already active. Unlocking them then is not harmless: each unlock byte
    lands as a flags write, and 'V' and 'I' carry the persist bit. Only a
    $D03F that reads $FF gets the sequence. On hardware the first sequence
    after a deactivate does not take, while the second always has; a stock
    VIC-II reads $FF through both."""
    flags = bus.read_memory(VIDEO_MEM_FLAGS, 1)
    if flags and flags[0] != LOCKED:
        return True
    for _attempt in range(2):
        for ch in UNLOCK:
            bus.write_memory(f"{VIDEO_MEM_FLAGS:04X}", f"{ch:02X}")
        bus.flush()
        flags = bus.read_memory(VIDEO_MEM_FLAGS, 1)
        if flags and flags[0] != LOCKED:
            return True
    return False


def probe(bus: MemoryBus) -> Kawari | None:
    """The run's `Kawari` when the C64's VIC is one that answers, else None.

    A hires mode an earlier run or program left on survives a C64 reset, and
    nothing c64cast draws shows through one, so it is turned off here. The
    extra registers are locked again before returning, and nothing is written
    with the persist flag, so the Kawari's saved settings are untouched.
    """
    if not _activate(bus):
        log.debug("Kawari: $D03F stayed locked, not a Kawari")
        return None
    try:
        info = _read_info(bus)
        if info is not None:
            _clear_hires(bus)
    finally:
        _deactivate(bus)
    return None if info is None else Kawari(bus, info)


def _read_info(bus: MemoryBus) -> KawariInfo | None:
    _write_flags(bus, FLAGS_OVERLAY)
    model = _overlay_byte(bus, OVERLAY_CHIP_MODEL)
    if model is None or model >= len(CHIP_MODELS):
        log.debug("Kawari: chip-model register read %r, not a Kawari", model)
        return None
    channels = [_overlay_byte(bus, offset) for offset in _PALETTE_OFFSETS]
    spare = [_overlay_byte(bus, offset) for offset in _SPARE_OFFSETS]
    version = [_overlay_byte(bus, OVERLAY_VERSION + i) for i in range(2)]
    cap_lo = _overlay_byte(bus, OVERLAY_CAP_LO)
    variant = [_overlay_byte(bus, OVERLAY_VARIANT + i) for i in range(VARIANT_BYTES)]
    answers = [*channels, *spare, *version, cap_lo, *variant]
    if any(v is None for v in answers):
        log.debug("Kawari: an overlay byte went unanswered")
        return None
    scaled = [to_8bit(c) for c in channels if c is not None]
    name = bytes(v for v in variant if v is not None).split(b"\0")[0]
    return KawariInfo(
        model=CHIP_MODELS[model],
        version=(version[0] or 0, version[1] or 0),
        variant=name.decode("latin-1"),
        cap_lo=cap_lo or 0,
        palette_rgb=tuple(
            (scaled[i], scaled[i + 1], scaled[i + 2]) for i in range(0, len(scaled), 3)
        ),
        palette_spare=tuple(v for v in spare if v is not None),
    )


def _clear_hires(bus: MemoryBus) -> None:
    mode = bus.read_memory(HIRES_MODE, 1)
    if mode and mode[0] != 0:
        log.info("Kawari: hires mode $%02X was left on; turning it off", mode[0])
        bus.write_memory(f"{HIRES_MODE:04X}", "00")
