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
from collections.abc import Sequence
from dataclasses import dataclass

from .uci import MemoryBus

log = logging.getLogger(__name__)

HIRES_MODE = 0xD037
VIDEO_MEM_1_ADDR = 0xD039  # port A address, lo then hi
VIDEO_MEM_1_VAL = 0xD03B
VIDEO_MEM_FLAGS = 0xD03F

UNLOCK = b"VIC2"
LOCKED = 0xFF
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

_PALETTE_OFFSETS = tuple(
    OVERLAY_PALETTE_RGB + 4 * color + channel
    for color in range(PALETTE_COLORS)
    for channel in range(3)
)


def to_8bit(channel: int) -> int:
    return round((channel & CHANNEL_MAX) * 255 / CHANNEL_MAX)


def to_6bit(channel: int) -> int:
    return round(channel * CHANNEL_MAX / 255)


@dataclass(frozen=True)
class KawariInfo:
    """What a Kawari said about itself when the run started. `palette_rgb` is
    16 RGB triples scaled to 8 bits a channel."""

    model: str
    version: tuple[int, int]
    variant: str
    cap_lo: int
    palette_rgb: tuple[tuple[int, int, int], ...]

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
    version = [_overlay_byte(bus, OVERLAY_VERSION + i) for i in range(2)]
    cap_lo = _overlay_byte(bus, OVERLAY_CAP_LO)
    variant = [_overlay_byte(bus, OVERLAY_VARIANT + i) for i in range(VARIANT_BYTES)]
    answers = [*channels, *version, cap_lo, *variant]
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
    )


def _clear_hires(bus: MemoryBus) -> None:
    mode = bus.read_memory(HIRES_MODE, 1)
    if mode and mode[0] != 0:
        log.info("Kawari: hires mode $%02X was left on; turning it off", mode[0])
        bus.write_memory(f"{HIRES_MODE:04X}", "00")
