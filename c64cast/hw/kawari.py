"""Reading a VIC-II Kawari's live palette over the cartridge bus.

The Kawari is an FPGA replacement for the VIC-II. Its extra registers sit at
$D02F-$D03F behind an unlock sequence, and its configuration — chip model and
the 16-color RGB palette its digital output drives among it — is reachable as
an "overlay" over the first 256 bytes of its video memory. The register map is
the project's own: `doc/REGISTERS.md` in randyrossi/vicii-kawari.

A stock VIC-II returns $FF from every one of those registers, so $D03F reading
anything else after the unlock, and then a chip-model byte of 0-3, is what
tells the two apart.
"""

from __future__ import annotations

import logging

from .uci import MemoryBus

log = logging.getLogger(__name__)

VIDEO_MEM_1_ADDR = 0xD039  # port A address, lo then hi
VIDEO_MEM_1_VAL = 0xD03B
VIDEO_MEM_FLAGS = 0xD03F

UNLOCK = b"VIC2"
LOCKED = 0xFF
FLAGS_OVERLAY = 0x20
FLAGS_DEACTIVATE = 0x80

OVERLAY_CHIP_MODEL = 0x1F
OVERLAY_PALETTE_RGB = 0x40  # 16 x (R, G, B, unused), 6 bits a channel
CHIP_MODELS = ("6567R8", "6569R3", "6567R56A", "6569R1")
PALETTE_COLORS = 16
CHANNEL_MAX = 63


def _overlay_byte(bus: MemoryBus, offset: int) -> int | None:
    bus.write_memory(f"{VIDEO_MEM_1_ADDR:04X}", f"{offset:02X}00")
    # On an Ultimate the write rides the DMA socket and the read is REST, so
    # the read could otherwise overtake it.
    bus.flush()
    data = bus.read_memory(VIDEO_MEM_1_VAL, 1)
    return data[0] if data else None


def _unlock(bus: MemoryBus) -> bool:
    """Send the unlock sequence until $D03F stops reading $FF, at most twice.

    On hardware, the first sequence after a deactivate does not take, while
    the second always has; a stock VIC-II reads $FF through both."""
    for _attempt in range(2):
        for ch in UNLOCK:
            bus.write_memory(f"{VIDEO_MEM_FLAGS:04X}", f"{ch:02X}")
        bus.flush()
        flags = bus.read_memory(VIDEO_MEM_FLAGS, 1)
        if flags and flags[0] != LOCKED:
            return True
    return False


def read_palette(bus: MemoryBus) -> tuple[str, tuple[tuple[int, int, int], ...]] | None:
    """``(chip model, 16 RGB triples)`` from a Kawari, or None when the machine
    is not one or did not answer.

    The extra registers are locked again before returning, and nothing is
    written with the persist flag, so the Kawari's saved settings are untouched.
    """
    if not _unlock(bus):
        log.debug("Kawari: $D03F stayed locked, not a Kawari")
        return None
    bus.write_memory(f"{VIDEO_MEM_FLAGS:04X}", f"{FLAGS_OVERLAY:02X}")
    model = _overlay_byte(bus, OVERLAY_CHIP_MODEL)
    known = model is not None and model < len(CHIP_MODELS)
    channels = [
        _overlay_byte(bus, OVERLAY_PALETTE_RGB + 4 * color + channel)
        for color in range(PALETTE_COLORS)
        for channel in range(3)
        if known
    ]
    bus.write_memory(f"{VIDEO_MEM_FLAGS:04X}", f"{FLAGS_DEACTIVATE:02X}")
    bus.flush()
    if model is None or not known:
        log.debug("Kawari: chip-model register read %r, not a Kawari", model)
        return None
    if any(c is None for c in channels):
        log.debug("Kawari: a palette byte went unanswered")
        return None
    scaled = [round((c & CHANNEL_MAX) * 255 / CHANNEL_MAX) for c in channels if c is not None]
    rgb = tuple((scaled[i], scaled[i + 1], scaled[i + 2]) for i in range(0, len(scaled), 3))
    return CHIP_MODELS[model], rgb
