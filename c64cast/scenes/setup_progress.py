"""The video-setup progress bar: a diagonal-striped strip that grows along
screen row ``BAR_ROW`` while ``VideoScene.setup()`` does its blocking work.

It paints via ``write_memory_file``, outside the ``write_region`` delta cache,
and never erases itself. ``bar_style_for`` supplies the per-mode fill and
returns None for an unknown or absent display mode.

See docs/architecture/scenes.md#setup_progresspy--the-video-setup-progress-bar.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Final, Literal

from c64cast.hw.backend import C64Backend
from c64cast.hw.c64 import SCREEN, VIC_BANK_0
from c64cast.video.modes.base import DisplayMode
from c64cast.video.modes.bitmap import BitmapDisplayMode
from c64cast.video.modes.char import CharDisplayMode
from c64cast.video.modes.mcm import MCMDisplayMode
from c64cast.video.modes.mhires import MultiHiresDisplayMode

BAR_ROW: Final = 22

_WHITE: Final = 1
# Char-mode fill: "/" from the uppercase glyph set (HatchStyle's diagonal).
_SC_DIAGONAL: Final = 0x4E
# MCM fill: in the synthesized charset, screen code i encodes a 2×2 crumb
# pattern — $C3 = top-left + bottom-right quadrants from color RAM.
_MCM_DIAGONAL: Final = 0xC3
# Color-RAM byte for the MCM fill: bit 3 flips the cell to multicolor,
# low bits pick the crumb-11 color (white).
_MCM_COLOR: Final = 0x08 | _WHITE
# One hires bitmap cell of 45° stripes: bit set where (x + y) % 4 == 0.
_STRIPE_CELL: Final = bytes((0x88, 0x11, 0x22, 0x44) * 2)
# Bitmap screen-RAM nibbles: hires draws set bits in the high nibble's
# color; mhires stripes alternate the 01/10 bit-pair sources, so both
# nibbles carry white to light every stripe pixel the same.
_HIRES_NIBBLES: Final = _WHITE << 4
_MHIRES_NIBBLES: Final = (_WHITE << 4) | _WHITE


@dataclass(frozen=True)
class BarStyle:
    """How to fill one bar cell in a given display-mode family."""

    kind: Literal["char", "bitmap"]
    fill_code: int  # char: the screen code; bitmap: unused (stripes)
    color_byte: int  # char: color-RAM byte; bitmap: screen-nibble byte


def bar_style_for(mode: DisplayMode | None) -> BarStyle | None:
    """The bar style for ``mode``, or None when no bar can be drawn.

    Subclass checks go most-specific first: MCM is a CharDisplayMode with
    its own charset semantics, mhires a BitmapDisplayMode with its own
    screen-nibble semantics."""
    if isinstance(mode, MCMDisplayMode):
        return BarStyle(kind="char", fill_code=_MCM_DIAGONAL, color_byte=_MCM_COLOR)
    if isinstance(mode, MultiHiresDisplayMode):
        return BarStyle(kind="bitmap", fill_code=0, color_byte=_MHIRES_NIBBLES)
    if isinstance(mode, BitmapDisplayMode):
        return BarStyle(kind="bitmap", fill_code=0, color_byte=_HIRES_NIBBLES)
    if isinstance(mode, CharDisplayMode):
        return BarStyle(kind="char", fill_code=_SC_DIAGONAL, color_byte=_WHITE)
    return None


class SetupProgressBar:
    """Paints the bar. ``show(fraction)`` quantizes to whole cells and only
    writes the newly filled span — repeated or shrinking fractions are
    no-ops, so callers can report freely."""

    def __init__(self, api: C64Backend, style: BarStyle, row: int = BAR_ROW):
        self.api = api
        self.style = style
        self.row = row
        self._cells = 0

    def show(self, fraction: float) -> None:
        cells = round(max(0.0, min(1.0, fraction)) * SCREEN.W_CHARS)
        if cells <= self._cells:
            return
        if self.style.kind == "char":
            self._extend_char(self._cells, cells)
        else:
            self._extend_bitmap(self._cells, cells)
        self._cells = cells

    def _extend_char(self, start: int, end: int) -> None:
        base = self.row * SCREEN.W_CHARS + start
        n = end - start
        self.api.write_memory_file(f"{SCREEN.RAM + base:04X}", bytes([self.style.fill_code]) * n)
        self.api.write_memory_file(
            f"{SCREEN.COLOR_RAM + base:04X}", bytes([self.style.color_byte]) * n
        )

    def _extend_bitmap(self, start: int, end: int) -> None:
        bitmap = VIC_BANK_0.BITMAP + self.row * SCREEN.BITMAP_W + start * 8
        nibbles = VIC_BANK_0.SCREEN + self.row * SCREEN.W_CHARS + start
        n = end - start
        self.api.write_memory_file(f"{bitmap:04X}", _STRIPE_CELL * n)
        self.api.write_memory_file(f"{nibbles:04X}", bytes([self.style.color_byte]) * n)


class SegmentedProgress:
    """Folds per-step progress into one overall fraction for the bar.

    Setup steps differ wildly in cost and only some have real denominators
    (pre-scan samples, REU upload bytes); the rest jump to done via
    ``complete()``. Each step gets a static weight; the overall fraction is
    the weight-averaged sum, per-segment monotonic so a noisy reporter can
    never walk the bar backward."""

    def __init__(self, segments: Sequence[tuple[str, float]], on_change: Callable[[float], None]):
        self._weights = dict(segments)
        self._fractions = dict.fromkeys(self._weights, 0.0)
        self._total = sum(self._weights.values()) or 1.0
        self._on_change = on_change

    @classmethod
    def off(cls) -> SegmentedProgress:
        """A null model: reporters are None, mutators no-op — call sites
        stay unconditional whether or not a bar is drawn."""
        return cls((), lambda _fraction: None)

    def reporter(self, name: str) -> Callable[[float], None] | None:
        """A per-step ``on_progress(fraction)`` callback, or None when the
        step isn't part of this run (leaf functions skip a None hook)."""
        if name not in self._weights:
            return None

        def report(fraction: float) -> None:
            self._advance(name, fraction)

        return report

    def complete(self, name: str) -> None:
        if name in self._weights:
            self._advance(name, 1.0)

    def finish(self) -> None:
        """Force the bar to the right edge — setup is done regardless of
        which segments reported."""
        for name in self._fractions:
            self._fractions[name] = 1.0
        self._on_change(1.0)

    def _advance(self, name: str, fraction: float) -> None:
        clamped = max(self._fractions[name], min(1.0, fraction))
        self._fractions[name] = clamped
        overall = sum(w * self._fractions[n] for n, w in self._weights.items()) / self._total
        self._on_change(overall)


def make_setup_bar(api: C64Backend, mode: DisplayMode | None) -> SetupProgressBar | None:
    """The bar for ``mode``'s screen, or None when the mode can't host one."""
    style = bar_style_for(mode)
    return None if style is None else SetupProgressBar(api, style)
