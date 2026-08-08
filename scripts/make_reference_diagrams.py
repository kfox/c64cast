#!/usr/bin/env python3
"""Draw the Programmer's Reference Guide's diagrams into docs/reference/img/.

    python scripts/make_reference_diagrams.py            # redraw everything
    python scripts/make_reference_diagrams.py ladder      # just that one

Five figures, for the five things in the book that are inherently spatial and
were carrying the whole load in prose: the precedence ladder, the twelve-step
display pipeline, the VIC-II's per-cell attribute story, the two audio paths,
and the 64 KB memory map.

Committed rather than built on demand. The release renders the books with
`uv run --no-project typst`, which cannot import c64cast or Pillow, so
anything a figure needs must already be a PNG in the tree. `make
reference-figures` regenerates them.

Drawn with Pillow and the vendored faces in docs/shared/fonts/, so a diagram
sits in the same type as the page around it. make_guide_figures.py's
cv2.putText path was the wrong model here -- a Hershey stroke font next to
Jost reads as a screenshot of a different document -- and
capture_guide_figure.py's PIL path reads a user-installed font, which makes
the output depend on the machine that drew it.

Everything is laid out in a 1500-wide design space and drawn at SS times that,
then downsampled: Pillow has no antialiasing of its own, and the isometric
map is nothing but diagonals.
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from c64cast.video.palette import C64_PALETTE_BGR  # noqa: E402

IMG_DIR = REPO_ROOT / "docs" / "reference" / "img"
FONT_DIR = REPO_ROOT / "docs" / "shared" / "fonts"
BODY_FONT = FONT_DIR / "Jost[wght].ttf"
MONO_FONT = FONT_DIR / "Inconsolata[wdth,wght].ttf"

# The template places a figure at 78% of a 4.60in measure, so 1500px across is
# about 420dpi -- which also sets the type: a 10pt label is 58px, not the 20px
# that looks right on a screen. Nothing here is smaller than 34px.
WIDTH = 1500
SS = 2

# docs/shared/template.typ's palette, duplicated here so a diagram is drawn in
# the same blue the page around it is set in. tests/test_reference_diagrams.py
# fails if the two ever disagree.
ACCENT = (0x2B, 0x73, 0xB5)
ACCENT_PALE = (0x9F, 0xC0, 0xDE)
ACCENT_WASH = (0xF2, 0xF6, 0xFA)
INK = (0x11, 0x11, 0x11)

PAPER = (0xFF, 0xFF, 0xFF)
MUTED = (0x6B, 0x6B, 0x6B)


# ---------------------------------------------------------------------------
# Drawing vocabulary
#
# Every helper takes design-space coordinates and scales them by SS on the way
# to Pillow, so the figures below read at the size they will be printed at.
# ---------------------------------------------------------------------------


_FONTS: dict[tuple[str, int, str], ImageFont.FreeTypeFont] = {}


def font(family: str, size: int, weight: str = "Regular") -> ImageFont.FreeTypeFont:
    """One of the two vendored faces, at a named variation weight."""
    key = (family, size, weight)
    cached = _FONTS.get(key)
    if cached is None:
        path = BODY_FONT if family == "body" else MONO_FONT
        cached = ImageFont.truetype(str(path), size * SS)
        cached.set_variation_by_name(weight)
        _FONTS[key] = cached
    return cached


def canvas(height: int) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    img = Image.new("RGB", (WIDTH * SS, height * SS), PAPER)
    return img, ImageDraw.Draw(img)


def finish(img: Image.Image) -> Image.Image:
    return img.resize((img.width // SS, img.height // SS), Image.Resampling.LANCZOS)


def _s(values: tuple[float, ...] | list[float]) -> list[float]:
    return [v * SS for v in values]


def text(
    d: ImageDraw.ImageDraw,
    xy: tuple[float, float],
    s: str,
    f: ImageFont.FreeTypeFont,
    fill: tuple[int, int, int] = INK,
    anchor: str = "la",
) -> None:
    d.text((xy[0] * SS, xy[1] * SS), s, font=f, fill=fill, anchor=anchor)


def text_width(s: str, f: ImageFont.FreeTypeFont) -> float:
    return f.getlength(s) / SS


def must_fit(width: float, limit: float, what: str) -> None:
    """Refuse to draw a line that would run off its box.

    A figure is committed and only ever looked at once, so an overlong label
    silently clipped at the edge is exactly the kind of thing that ships.
    """
    if width > limit:
        raise SystemExit(f"{what!r} overruns its figure by {width - limit:.0f}px")


def rich_text(
    d: ImageDraw.ImageDraw,
    xy: tuple[float, float],
    runs: list[tuple[str, ImageFont.FreeTypeFont, tuple[int, int, int]]],
    anchor: str = "lm",
) -> None:
    """One line built from runs in different faces.

    An address belongs in Inconsolata and the sentence around it does not, and
    a figure that sets the whole line in one face to avoid this reads as a
    terminal transcript rather than as part of the book.
    """
    total = sum(text_width(s, f) for s, f, _ in runs)
    x = xy[0] - (total if anchor[0] == "r" else total / 2 if anchor[0] == "m" else 0)
    for s, f, fill in runs:
        text(d, (x, xy[1]), s, f, fill, anchor=f"l{anchor[1]}")
        x += text_width(s, f)


def box(
    d: ImageDraw.ImageDraw,
    rect: tuple[float, float, float, float],
    fill: tuple[int, int, int] | None = None,
    outline: tuple[int, int, int] | None = ACCENT_PALE,
    width: float = 1.5,
    radius: float = 10,
) -> None:
    d.rounded_rectangle(
        _s(rect), radius=radius * SS, fill=fill, outline=outline, width=max(1, round(width * SS))
    )


def line(
    d: ImageDraw.ImageDraw,
    points: list[tuple[float, float]],
    fill: tuple[int, int, int] = ACCENT,
    width: float = 2,
) -> None:
    flat = [c * SS for p in points for c in p]
    d.line(flat, fill=fill, width=max(1, round(width * SS)), joint="curve")


def dashed(
    d: ImageDraw.ImageDraw,
    p0: tuple[float, float],
    p1: tuple[float, float],
    fill: tuple[int, int, int] = ACCENT_PALE,
    width: float = 2,
    dash: float = 12,
) -> None:
    span = math.dist(p0, p1)
    steps = max(1, int(span // dash))
    for i in range(0, steps, 2):
        a = i / steps
        b = min(1.0, (i + 1) / steps)
        line(
            d,
            [
                (p0[0] + (p1[0] - p0[0]) * a, p0[1] + (p1[1] - p0[1]) * a),
                (p0[0] + (p1[0] - p0[0]) * b, p0[1] + (p1[1] - p0[1]) * b),
            ],
            fill,
            width,
        )


def dashed_box(
    d: ImageDraw.ImageDraw,
    rect: tuple[float, float, float, float],
    fill: tuple[int, int, int] | None = None,
    outline: tuple[int, int, int] = ACCENT,
    width: float = 2,
) -> None:
    x0, y0, x1, y1 = rect
    if fill is not None:
        box(d, rect, fill=fill, outline=None)
    for a, b in (
        ((x0, y0), (x1, y0)),
        ((x1, y0), (x1, y1)),
        ((x1, y1), (x0, y1)),
        ((x0, y1), (x0, y0)),
    ):
        dashed(d, a, b, outline, width)


def arrow(
    d: ImageDraw.ImageDraw,
    p0: tuple[float, float],
    p1: tuple[float, float],
    fill: tuple[int, int, int] = ACCENT,
    width: float = 3,
    head: float = 14,
) -> None:
    line(d, [p0, p1], fill, width)
    angle = math.atan2(p1[1] - p0[1], p1[0] - p0[0])
    for spread in (2.6, -2.6):
        d.line(
            _s(
                (
                    p1[0],
                    p1[1],
                    p1[0] + head * math.cos(angle + spread),
                    p1[1] + head * math.sin(angle + spread),
                )
            ),
            fill=fill,
            width=max(1, round(width * SS)),
        )


def chip(d: ImageDraw.ImageDraw, center: tuple[float, float], r: float, label: str) -> None:
    """The numbered accent disc that leads a step or a rung."""
    cx, cy = center
    d.ellipse(_s((cx - r, cy - r, cx + r, cy + r)), fill=ACCENT)
    text(d, (cx, cy + 1), label, font("body", round(r * 1.25), "SemiBold"), PAPER, anchor="mm")


def rotated_text(
    img: Image.Image,
    xy: tuple[float, float],
    s: str,
    f: ImageFont.FreeTypeFont,
    fill: tuple[int, int, int] = MUTED,
) -> None:
    """A label set up the side of a figure, centered on `xy` after rotation."""
    w, h = round(f.getlength(s)) + 8 * SS, round(f.size * 1.6)
    strip = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(strip).text((w // 2, h // 2), s, font=f, fill=fill, anchor="mm")
    strip = strip.rotate(90, expand=True)
    img.paste(
        strip, (round(xy[0] * SS - strip.width // 2), round(xy[1] * SS - strip.height // 2)), strip
    )


def c64(index: int) -> tuple[int, int, int]:
    """A C64 palette entry as RGB. C64_PALETTE_BGR is OpenCV order."""
    b, g, r = C64_PALETTE_BGR[index]
    return (int(r), int(g), int(b))


# ---------------------------------------------------------------------------
# Figure 1-1 — the precedence ladder
# ---------------------------------------------------------------------------

# Bottom rung first. The ensemble's master cascade has no number because it is
# not one of the five: it is an extra rung, and only on an ensemble run. The
# last field says whether the right-hand column is something you could type.
_LADDER = [
    ("1", "The built-in default", "what Appendix A prints", False),
    ("2", "Machine settings", "~/.config/c64cast/settings.toml", True),
    ("3", "The configuration file", "c64cast.toml", True),
    ("", "The master cascade", "ensemble runs only", False),
    ("4", "Command-line flags", "--sample-rate 12000", True),
    ("5", "The environment", "C64CAST_DMA_PASSWORD", True),
]


def fig_ladder() -> Image.Image:
    row_h, gap, top = 118, 20, 60
    height = top * 2 + row_h * len(_LADDER) + gap * (len(_LADDER) - 1)
    img, d = canvas(height)

    x0, x1 = 200, 1430
    name_f = font("body", 46, "SemiBold")
    src_f = font("mono", 40)
    note_f = font("body", 38)

    for i, (number, name, source, literal) in enumerate(_LADDER):
        y = height - top - row_h - i * (row_h + gap)
        cascade = not number
        rect = (x0 + (46 if cascade else 0), y, x1, y + row_h)
        if cascade:
            dashed_box(d, rect, fill=PAPER, outline=ACCENT_PALE, width=2)
        else:
            box(d, rect, fill=ACCENT_WASH, outline=ACCENT_PALE)
            chip(d, (x0 + 62, y + row_h / 2), 34, number)
        text(d, (x0 + (100 if cascade else 168), y + row_h / 2), name, name_f, anchor="lm")
        text(
            d,
            (x1 - 34, y + row_h / 2),
            source,
            src_f if literal else note_f,
            ACCENT if literal else MUTED,
            anchor="rm",
        )

    arrow(d, (110, height - top - 10), (110, top + 10), ACCENT, 4, 18)
    rotated_text(
        img, (66, height / 2), "each rung beats the ones below it", font("body", 36), MUTED
    )
    return finish(img)


# ---------------------------------------------------------------------------
# Figure 3-1 — one cell per display mode
#
# The only figure drawn in C64 colors rather than the book's: what it is about
# is which palette entry each attribute byte holds.
# ---------------------------------------------------------------------------

# A hires cell is 8x8 one-bit pixels; the multicolor modes halve that to 4x8
# two-bit pixels. Both are spelled as rows of digits, one character per pixel.
_GLYPH_A = [
    "00011000",
    "00111100",
    "01100110",
    "01111110",
    "01100110",
    "01100110",
    "01100110",
    "00000000",
]
_HIRES_ART = [
    "00111100",
    "01111110",
    "11100111",
    "11000011",
    "11000011",
    "11100111",
    "01111110",
    "00111100",
]
_MCM_ART = ["0110", "1221", "2332", "2332", "2332", "1221", "0110", "0000"]
_MHIRES_ART = ["0012", "0123", "1233", "2331", "3312", "3120", "1200", "2000"]


def _draw_cell(
    d: ImageDraw.ImageDraw,
    origin: tuple[float, float],
    art: list[str],
    colors: dict[str, tuple[int, int, int]],
    size: float = 288,
) -> None:
    """One 8x8 hardware cell blown up, each pixel outlined so the grid reads."""
    rows, cols = len(art), len(art[0])
    pw, ph = size / cols, size / rows
    ox, oy = origin
    for r, row in enumerate(art):
        for c, key in enumerate(row):
            d.rectangle(
                _s((ox + c * pw, oy + r * ph, ox + (c + 1) * pw, oy + (r + 1) * ph)),
                fill=colors[key],
                outline=(0x33, 0x33, 0x33),
                width=1,
            )
    box(d, (ox, oy, ox + size, oy + size), fill=None, outline=INK, width=2, radius=0)


# mode -> (subtitle, cell art, pixel colors, attribute lines)
_CELL_PANELS: list[tuple[str, str, list[str], dict[str, tuple[int, int, int]], list[str]]] = [
    (
        "petscii",
        "8 × 8 pixels — one glyph from the character ROM, in one color",
        _GLYPH_A,
        {"0": c64(6), "1": c64(1)},
        [
            "$0400+n   the screen code — which glyph",
            "$D800+n   the cell's color, any of the 16",
            "$D021     the background, shared by the screen",
        ],
    ),
    (
        "mcm",
        "4 × 8 double-wide pixels — 3 shared colors, 1 of its own",
        _MCM_ART,
        {"0": c64(0), "1": c64(6), "2": c64(14), "3": c64(3)},
        [
            "00 → $D021    01 → $D022    10 → $D023    all shared",
            "11 → $D800+n  the cell's foreground, one of the first 8",
            "c64cast's charset divides the cell into 2 × 2 blocks",
        ],
    ),
    (
        "hires",
        "8 × 8 pixels, one bit each — two colors, held in one byte's nibbles",
        _HIRES_ART,
        {"0": c64(0), "1": c64(1)},
        [
            "$2000+8n  eight bitmap bytes, one bit to the pixel",
            "$0400+n   high nibble — the color of the 1 bits",
            "$0400+n   low nibble  — the color of the 0 bits",
        ],
    ),
    (
        "mhires",
        "4 × 8 double-wide pixels — 1 shared color, 3 of its own",
        _MHIRES_ART,
        {"0": c64(0), "1": c64(4), "2": c64(14), "3": c64(1)},
        [
            "$2000+8n  eight bitmap bytes, two bits to the pixel",
            "00 → $D021 (shared)   01 → $0400+n hi   10 → $0400+n lo",
            "11 → $D800+n  the third color, from color RAM",
        ],
    ),
]

CELL_ART = 170.0


def fig_cells() -> Image.Image:
    margin, gutter, row_h = 40, 24, 276
    height = margin * 2 + row_h * len(_CELL_PANELS) + gutter * (len(_CELL_PANELS) - 1)
    img, d = canvas(height)

    mode_f = font("mono", 42, "SemiBold")
    sub_f = font("body", 36)
    attr_f = font("mono", 38)

    for i, (mode, subtitle, art, colors, attrs) in enumerate(_CELL_PANELS):
        py = margin + i * (row_h + gutter)
        box(d, (margin, py, WIDTH - margin, py + row_h), fill=ACCENT_WASH, outline=ACCENT_PALE)

        _draw_cell(d, (margin + 24, py + 52), art, colors, CELL_ART)

        tx = margin + 24 + CELL_ART + 30
        room = WIDTH - margin - 24 - tx
        title = [(mode, mode_f, ACCENT), ("   " + subtitle, sub_f, MUTED)]
        must_fit(sum(text_width(s, f) for s, f, _ in title), room, subtitle)
        rich_text(d, (tx, py + 48), title, anchor="lm")
        for j, attr in enumerate(attrs):
            must_fit(text_width(attr, attr_f), room, attr)
            text(d, (tx, py + 116 + j * 56), attr, attr_f, INK)

    return finish(img)


# ---------------------------------------------------------------------------
# Figure 3-2 — from frame to screen
# ---------------------------------------------------------------------------

# step -> (what happens, what enters there, is it something you could type)
_PIPELINE = [
    ("The source produces a frame", "", True),
    ("Fitted to the Commodore's aspect", "aspect_mode", True),
    ("The effect chain runs", "[[effects]]", True),
    ("Downscaled to the mode's grid", "display", True),
    ("Colors are shaped", "channel_boost · hue_corrections · auto_fit", True),
    ("A forced palette is applied", "force_palette · force_palette_colors", True),
    ("Dithering", "dither", True),
    ("Every pixel matched to the palette", "color_match", True),
    ("Slots allocated, buffers packed", "palette_mode · cell_strategy", True),
    ("Overlays fold in", "[[overlays]]", True),
    ("A fade or a dim is applied", "fade_duration_s", True),
    ("The buffers are pushed", "changed bytes only", False),
]

# Step 8 is where the frame stops being a picture and becomes sixteen colors.
_QUANTIZE_STEP = 8


def fig_pipeline() -> Image.Image:
    row_h, gap, top = 92, 12, 56
    height = top * 2 + row_h * len(_PIPELINE) + gap * (len(_PIPELINE) - 1)
    img, d = canvas(height)

    x0, x1 = 126, 1460
    step_f = font("body", 42)
    key_f = font("mono", 38)
    note_f = font("body", 38)

    for i, (what, keys, literal) in enumerate(_PIPELINE):
        y = top + i * (row_h + gap)
        mid = y + row_h / 2
        box(d, (x0, y, x1, y + row_h), fill=ACCENT_WASH if keys else PAPER, outline=ACCENT_PALE)
        if i:
            arrow(d, (x0 + 52, y - gap - 4), (x0 + 52, y + 4), ACCENT_PALE, 2.5, 9)
        chip(d, (x0 + 52, mid), 30, str(i + 1))
        text(d, (x0 + 116, mid), what, step_f, INK, anchor="lm")
        if keys:
            written = text_width(what, step_f) + text_width(keys, key_f if literal else note_f)
            must_fit(written, x1 - 28 - (x0 + 116) - 24, f"{what} / {keys}")
            text(
                d,
                (x1 - 28, mid),
                keys,
                key_f if literal else note_f,
                ACCENT if literal else MUTED,
                anchor="rm",
            )

    split = top + (_QUANTIZE_STEP - 1) * (row_h + gap) - gap / 2
    for y0, y1, label in (
        (top, split, "the frame is still full color"),
        (split, height - top, "sixteen colors, in the VIC's layout"),
    ):
        line(d, [(90, y0 + 6), (74, y0 + 6), (74, y1 - 6), (90, y1 - 6)], ACCENT_PALE, 2)
        rotated_text(img, (40, (y0 + y1) / 2), label, font("body", 34), MUTED)
    return finish(img)


# ---------------------------------------------------------------------------
# Figure 4-1 — the two ways out
# ---------------------------------------------------------------------------

# Each step is (address or nothing, the rest of the line).
_DAC_PATH = [
    ("", "The link, ahead of the read head"),
    ("$4000–$5FFF", "an 8 KB ring in RAM"),
    ("", "A timer interrupt, one per sample"),
    ("$D418", "the SID's volume register"),
    ("", "Audio out, through the SID"),
]
_SAMPLER_PATH = [
    ("", "The link, ahead of the read head"),
    ("$200000", "a 1 MiB ring in the REU"),
    ("", "An FPGA PCM channel, off the bus"),
    ("", "Line out, touching no SID register"),
]


def _stack(
    d: ImageDraw.ImageDraw,
    x0: float,
    x1: float,
    y0: float,
    y1: float,
    steps: list[tuple[str, str]],
) -> None:
    """Boxes spread evenly down a band, joined by arrows."""
    mono, body = font("mono", 36), font("body", 38)
    n = len(steps)
    gap = 46
    h = (y1 - y0 - gap * (n - 1)) / n
    for i, (addr, what) in enumerate(steps):
        y = y0 + i * (h + gap)
        box(d, (x0, y, x1, y + h), fill=ACCENT_WASH, outline=ACCENT_PALE)
        runs = ([(addr, mono, ACCENT), ("  ", body, INK)] if addr else []) + [(what, body, INK)]
        must_fit(sum(text_width(s, f) for s, f, _ in runs), x1 - x0 - 44, f"{addr} {what}")
        rich_text(d, ((x0 + x1) / 2, y + h / 2), runs, anchor="mm")
        if i:
            arrow(d, ((x0 + x1) / 2, y - gap + 6), ((x0 + x1) / 2, y - 6), ACCENT, 3, 13)


def fig_audio() -> Image.Image:
    height = 1240
    img, d = canvas(height)

    left = (60.0, 720.0)
    right = (780.0, 1440.0)
    head_f = font("body", 48, "SemiBold")
    cost_f = font("body", 38)

    box(d, (left[0], 60, right[1], 168), fill=ACCENT_WASH, outline=ACCENT_PALE)
    text(
        d,
        ((left[0] + right[1]) / 2, 114),
        "c64cast on the host — decode, resample, compand",
        font("body", 44),
        INK,
        anchor="mm",
    )

    for band, title, rate, steps in (
        (left, "4-bit DAC", "12 kHz · 4 bits", _DAC_PATH),
        (right, "Ultimate Audio sampler", "44.1 kHz · 16 bits", _SAMPLER_PATH),
    ):
        cx = (band[0] + band[1]) / 2
        arrow(d, (cx, 178), (cx, 236), ACCENT, 3, 13)
        text(d, (cx, 268), title, head_f, ACCENT, anchor="mm")
        text(d, (cx, 320), rate, font("mono", 38), MUTED, anchor="mm")
        _stack(d, band[0], band[1], 356, 1000, steps)

    for band, cost in (
        (left, "Costs the 6510 an interrupt\nper sample, and shares the\nlink with the picture"),
        (right, "Costs the 6510 nothing at all.\nThe picture keeps the full\nframe rate"),
    ):
        box(d, (band[0], 1052, band[1], 1180), fill=PAPER, outline=ACCENT)
        text(d, ((band[0] + band[1]) / 2, 1116), cost, cost_f, INK, anchor="mm")

    return finish(img)


# ---------------------------------------------------------------------------
# Figure 5-1 — what lands in memory
#
# Isometric because the VIC's banks genuinely are parallel 16 KB windows over
# one address space, and a flat map cannot say that: it has to draw either the
# address space or the banks, and the thing worth showing is that color RAM
# sits in neither.
# ---------------------------------------------------------------------------

BANK_BYTES = 0x4000
SLAB_LEN = 460.0  # a whole 16 KB bank, along the address axis
SLAB_DEPTH = 120.0
SLAB_H = 42.0  # how thick the bar of memory is drawn

# A slab's *projected* height is (LEN + DEPTH) * sin30 + H, and a rhombus
# overlaps a copy of itself shifted up by any less than that. Draw the banks
# closer together than this and each one crosses the one below it, which is an
# Escher staircase rather than a memory map.
SLAB_RISE = 355.0
_COS30, _SIN30 = math.cos(math.radians(30)), 0.5


def _iso(u: float, v: float, w: float, origin: tuple[float, float]) -> tuple[float, float]:
    return (origin[0] + (u - v) * _COS30, origin[1] - (u + v) * _SIN30 - w)


def _addr_u(addr: int) -> float:
    """Where an address sits along a bank, as a fraction of its 16 KB."""
    return (addr % BANK_BYTES) / BANK_BYTES * SLAB_LEN


def _shade(color: tuple[int, int, int], factor: float) -> tuple[int, int, int]:
    return tuple(min(255, round(c * factor)) for c in color)  # type: ignore[return-value]


def _face(
    d: ImageDraw.ImageDraw, points: list[tuple[float, float]], fill: tuple[int, int, int]
) -> None:
    d.polygon([c * SS for p in points for c in p], fill=fill)


def _slab_segment(
    d: ImageDraw.ImageDraw,
    origin: tuple[float, float],
    u0: float,
    u1: float,
    color: tuple[int, int, int],
    length: float = SLAB_LEN,
    depth: float = SLAB_DEPTH,
) -> tuple[float, float]:
    """Color the run of addresses u0..u1 through a slab. Returns its top center.

    A region is a section of the bar rather than a block standing on it. Drawn
    the other way round -- an extruded block over a flat plate -- each region
    reads as an L hovering above the memory instead of being part of it.
    """
    top = [
        _iso(u0, 0, SLAB_H, origin),
        _iso(u1, 0, SLAB_H, origin),
        _iso(u1, depth, SLAB_H, origin),
        _iso(u0, depth, SLAB_H, origin),
    ]
    front = [_iso(u0, 0, 0, origin), _iso(u1, 0, 0, origin), top[1], top[0]]
    _face(d, front, _shade(color, 0.74))
    _face(d, top, color)
    if u0 <= 0:  # the left end cap, visible only on the first segment
        cap = [_iso(0, 0, 0, origin), _iso(0, depth, 0, origin), top[3], top[0]]
        _face(d, cap, _shade(color, 0.86))
    if u1 >= length:  # and the right one, on the last
        cap = [_iso(length, 0, 0, origin), _iso(length, depth, 0, origin), top[2], top[1]]
        _face(d, cap, _shade(color, 0.86))
    return ((top[0][0] + top[2][0]) / 2, (top[0][1] + top[2][1]) / 2)


def _slab_outline(
    d: ImageDraw.ImageDraw,
    origin: tuple[float, float],
    length: float = SLAB_LEN,
    depth: float = SLAB_DEPTH,
) -> None:
    """The bar's silhouette and its two visible internal edges."""
    corners = [
        _iso(0, 0, 0, origin),
        _iso(length, 0, 0, origin),
        _iso(length, 0, SLAB_H, origin),
        _iso(length, depth, SLAB_H, origin),
        _iso(0, depth, SLAB_H, origin),
        _iso(0, 0, SLAB_H, origin),
    ]
    line(d, corners + [corners[0]], ACCENT_PALE, 1.6)
    line(d, [_iso(0, 0, SLAB_H, origin), _iso(length, 0, SLAB_H, origin)], ACCENT_PALE, 1.6)
    line(d, [_iso(0, 0, 0, origin), _iso(0, 0, SLAB_H, origin)], ACCENT_PALE, 1.6)


# The book is set in one blue, so the regions are told apart by what they are
# for rather than by hue: the picture in the accent, its double-buffered copy
# in the pale one, everything the 6510 runs in gray.
_PICTURE = ACCENT
_SPARE = ACCENT_PALE
_CODE = (0x77, 0x7C, 0x82)
_SOUND = (0x3E, 0x46, 0x50)

# bank -> (label, its base address, [(start, end, color, name, address, label y
# relative to the plate's own origin)]). The label heights are set by hand:
# screen RAM and the BASIC program are 1 KB apart in a 16 KB bank, so their
# natural label positions are five pixels apart.
_MEMORY: list[tuple[str, str, list[tuple[int, int, tuple[int, int, int], str, str, float]]]] = [
    (
        "VIC bank 0",
        "$0000",
        [
            (0x0400, 0x07E7, _PICTURE, "Screen RAM", "$0400–$07E7", -30),
            (0x0801, 0x0A00, _CODE, "The BASIC program", "$0801", -140),
            (0x2000, 0x3F3F, _PICTURE, "Bitmap, 8 KB", "$2000–$3F3F", -250),
        ],
    ),
    (
        "VIC bank 1",
        "$4000",
        [(0x4000, 0x5FFF, _SOUND, "The audio ring, 8 KB", "$4000–$5FFF", -120)],
    ),
    (
        "VIC bank 2",
        "$8000",
        [
            (0x8400, 0x87E7, _SPARE, "Screen RAM, spare bank", "$8400–$87E7", -30),
            (0xA000, 0xBF3F, _SPARE, "Bitmap, spare bank", "$A000–$BF3F", -250),
        ],
    ),
    (
        "VIC bank 3",
        "$C000",
        [
            (0xC020, 0xC2FF, _SOUND, "The audio handlers", "$C020–$C2FF", -30),
            (0xC300, 0xC70F, _CODE, "The SID player and friends", "$C300–$C70F", -140),
        ],
    ),
]

# A 32-byte interrupt handler is a twentieth of a pixel at 600px to the bank,
# so every region gets a floor. The caption says the map is schematic.
MIN_REGION = 22.0

# Labels sit in a column clear of the stack. A horizontal run at any plate's
# own height passes under every plate above it, so a leader out to this column
# never crosses one.
LABEL_X = 980.0
ELBOW_X = 900.0


def fig_memory() -> Image.Image:
    height = 1700
    img, d = canvas(height)

    base = (392.0, height - 250.0)
    bank_f = font("body", 44, "SemiBold")
    addr_f = font("mono", 36)
    name_f = font("body", 40)
    note_f = font("body", 36)

    for i, (bank, bank_addr, regions) in enumerate(_MEMORY):
        origin = (base[0], base[1] - i * SLAB_RISE)
        _slab_segment(d, origin, 0, SLAB_LEN, ACCENT_WASH)

        edge = _iso(0, SLAB_DEPTH, SLAB_H, origin)
        text(d, (edge[0] - 24, edge[1] - 22), bank, bank_f, INK, anchor="rm")
        text(d, (edge[0] - 24, edge[1] + 18), bank_addr, addr_f, MUTED, anchor="rm")

        for start, end, color, name, addr, label_y in regions:
            u0 = _addr_u(start)
            u1 = max(u0 + MIN_REGION, _addr_u(end))
            center = _slab_segment(d, origin, u0, u1, color)
            ly = origin[1] + label_y
            line(
                d,
                [center, (ELBOW_X, center[1]), (ELBOW_X, ly), (LABEL_X - 16, ly)],
                ACCENT_PALE,
                1.5,
            )
            text(d, (LABEL_X, ly - 34), name, name_f, INK, anchor="ls")
            text(d, (LABEL_X, ly + 6), addr, addr_f, ACCENT, anchor="la")
        _slab_outline(d, origin)

    # Color RAM is drawn as a region with no plate under it, because that is
    # the fact worth drawing: there is one of it, it belongs to no bank, and
    # the VIC reads it whichever bank is displayed. Leader lines to the two
    # screen RAMs were tried and had to cross the label column to get there.
    cr = (330.0, height - 40.0)
    _slab_segment(d, cr, 0, 200, _PICTURE, length=200, depth=SLAB_DEPTH)
    _slab_outline(d, cr, length=200, depth=SLAB_DEPTH)
    text(d, (560, height - 210), "Color RAM", name_f, INK, anchor="ls")
    text(d, (560, height - 170), "$D800–$DBE7", addr_f, ACCENT, anchor="la")
    text(
        d,
        (560, height - 116),
        "in no VIC bank at all — the VIC reads it\nout of whichever bank is displayed",
        note_f,
        MUTED,
        anchor="la",
    )

    return finish(img)


# ---------------------------------------------------------------------------
# The shot list
# ---------------------------------------------------------------------------

# name -> (drawing function, which chapter it belongs to, what it shows)
FIGURES = {
    "fig-1-1-ladder": (
        fig_ladder,
        "1 · The Precedence Ladder",
        "The five layers, and the rung an ensemble inserts",
    ),
    "fig-3-1-pipeline": (
        fig_pipeline,
        "3 · From Frame to Screen",
        "The twelve steps, and where each setting enters",
    ),
    "fig-3-2-cells": (
        fig_cells,
        "3 · The Six Display Modes",
        "One hardware cell per mode, with the bytes that color it",
    ),
    "fig-4-1-audio": (
        fig_audio,
        "4 · Two Ways Out",
        "The DAC path against the sampler path, and what each costs",
    ),
    "fig-5-1-memory": (
        fig_memory,
        "5 · What Lands in Memory",
        "The 64 KB during a bitmap scene, banks stacked",
    ),
}


def write_shot_list() -> None:
    lines = [
        "# Reference guide diagrams",
        "",
        "Every image here is drawn by",
        "[`scripts/make_reference_diagrams.py`](../../../scripts/make_reference_diagrams.py)",
        "and committed, because the release renders the books with",
        "`uv run --no-project` and cannot regenerate anything that imports",
        "`c64cast`. Redraw them with `make reference-figures` after changing a",
        "diagram, and commit the result.",
        "",
        "These are drawings rather than captures — the guide's `img/` is the",
        "other kind. They are set in the books' own two faces from",
        "`docs/shared/fonts/` and use the template's palette, except inside the",
        "cell diagram, whose subject is which C64 color each attribute byte",
        "holds.",
        "",
        "| Figure | Chapter and section | Shows |",
        "|---|---|---|",
    ]
    for name, (_, where, shows) in FIGURES.items():
        lines.append(f"| `{name}.png` | {where} | {shows} |")
    lines += [
        "",
        "The memory map is schematic in one respect: a region the size of an",
        "interrupt handler is a fraction of a pixel wide at 16 KB to the plate,",
        "so every region is drawn at a minimum width.",
        "",
    ]
    (IMG_DIR / "README.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("only", nargs="*", choices=sorted(FIGURES), help="draw only these")
    args = ap.parse_args()

    IMG_DIR.mkdir(parents=True, exist_ok=True)
    wanted = args.only or list(FIGURES)
    for name in wanted:
        draw, _, _ = FIGURES[name]
        path = IMG_DIR / f"{name}.png"
        image = draw()
        image.save(path, optimize=True)
        size_kb = path.stat().st_size / 1024
        print(f"  {path.relative_to(REPO_ROOT)}  {image.width}x{image.height}  {size_kb:.0f} KB")

    write_shot_list()
    print(f"shot list: {(IMG_DIR / 'README.md').relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
