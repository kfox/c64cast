"""Decode what ``GET /v1/machine:menu_screen`` returns: the Ultimate menu's own
40x25 screen, as 1000 character bytes in reading order and then 1000 color
bytes.

The character bytes are not C64 screen codes. The menu draws in its own font,
whose printable range is ASCII, and the firmware stores ``c & 0x7F`` with bit 7
set for reverse video (``Screen_MemMappedCharMatrix::output_raw``). Codes below
``0x20`` are the font's line-drawing glyphs (``software/io/c64/screen.h``). A
color byte is ``fg | (bg << 4)``.

See docs/architecture/hardware-io.md#apipy--ultimate64api--socket_dmapy--socketdmaclient.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

COLUMNS: Final = 40
ROWS: Final = 25
CELLS: Final = COLUMNS * ROWS
PAYLOAD_BYTES: Final = 2 * CELLS

_REVERSE_BIT: Final = 0x80

# The menu font's glyphs below 0x20, from the CHR_* names in the firmware's
# screen.h. 0x08, 0x0A and 0x0D are marked "not usable" there and never drawn.
_GLYPHS: Final = {
    0x01: "┘",  # CHR_LOWER_RIGHT_CORNER
    0x02: "─",  # CHR_HORIZONTAL_LINE
    0x03: "└",  # CHR_LOWER_LEFT_CORNER
    0x04: "│",  # CHR_VERTICAL_LINE
    0x05: "┐",  # CHR_UPPER_RIGHT_CORNER
    0x06: "┌",  # CHR_UPPER_LEFT_CORNER
    0x07: "╯",  # CHR_ROUNDED_LOWER_RIGHT
    0x09: "╰",  # CHR_ROUNDED_LOWER_LEFT
    0x0B: "▄",  # CHR_SOLID_BAR_LOWER_7
    0x0C: "┤",  # CHR_ROW_LINE_RIGHT
    0x0E: "╮",  # CHR_ROUNDED_UPPER_RIGHT
    0x0F: "╭",  # CHR_ROUNDED_UPPER_LEFT
    0x10: "α",  # CHR_ALPHA
    0x11: "β",  # CHR_BETA
    0x12: "▀",  # CHR_SOLID_BAR_UPPER_7
    0x13: "◆",  # CHR_DIAMOND
}


@dataclass(frozen=True)
class MenuScreen:
    """One decoded capture. `lines` are the 25 rows as text; `reverse[r][c]` is
    True where the cell is drawn in reverse video; `colors` is the raw color
    plane, ``fg | (bg << 4)`` per cell in reading order."""

    lines: tuple[str, ...]
    reverse: tuple[tuple[bool, ...], ...]
    colors: bytes

    def text(self) -> str:
        """The screen as plain text, trailing spaces trimmed per row."""
        return "\n".join(line.rstrip() for line in self.lines)


def _char(code: int) -> str:
    if 0x20 <= code < 0x7F:
        return chr(code)
    return _GLYPHS.get(code, "?")


def decode_menu_screen(payload: bytes) -> MenuScreen:
    """Decode the 2000-byte attachment. Raises ValueError for any other size,
    which is what a firmware that changed the format would send."""
    if len(payload) != PAYLOAD_BYTES:
        raise ValueError(f"menu screen payload is {len(payload)} bytes, expected {PAYLOAD_BYTES}")
    lines = []
    reverse = []
    for row in range(ROWS):
        cells = payload[row * COLUMNS : (row + 1) * COLUMNS]
        lines.append("".join(_char(b & ~_REVERSE_BIT) for b in cells))
        reverse.append(tuple(bool(b & _REVERSE_BIT) for b in cells))
    return MenuScreen(lines=tuple(lines), reverse=tuple(reverse), colors=payload[CELLS:])
