"""Every character in every book must exist in a font the repository ships.

The books are set in two vendored faces and nothing else: docs/shared/template.typ
names Jost and Inconsolata and turns Typst's own fallback off, so a character
in neither is not substituted from the build machine -- it is simply not drawn.
Typst does not fail, does not warn, and the gap is a few points wide on one
page of two hundred.

That is not hypothetical. Jost has no U+2713 CHECK MARK and no U+2192 RIGHTWARDS
ARROW, which the generated appendices use eighty-odd times between them; before
the fallback was pinned, the compatibility matrix was set in whatever check the
builder happened to have installed, and the PDF from CI did not match the one
from a laptop. Both of those are now drawn by the template rather than set, and
this test is what stops the next one from shipping silently: a `⇒` reached
Appendix E from a docstring, and neither vendored face has it.

The cmap reader is stdlib `struct` on purpose. fontTools would do this in three
lines, but it reaches the test environment as a transitive dependency of
something else, and a guard that silently stops running when an unrelated
package moves is worse than no guard.
"""

from __future__ import annotations

import importlib.util
import os
import re
import struct
import sys
import unittest
from pathlib import Path

_REPO_ROOT = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_DOCS = _REPO_ROOT / "docs"
_FONT_DIR = _DOCS / "shared" / "fonts"
_TEMPLATE = _DOCS / "shared" / "template.typ"
_BOOK_DIRS = sorted(p.parent for p in _DOCS.glob("*/book.toml"))


def _load_build_book():
    path = _REPO_ROOT / "scripts" / "build_book.py"
    spec = importlib.util.spec_from_file_location("build_book", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


bg = _load_build_book()


# ---------------------------------------------------------------------------
# A minimal TrueType cmap reader
# ---------------------------------------------------------------------------


def _tables(data: bytes) -> dict[str, int]:
    """Offset of each sfnt table, by tag."""
    count = struct.unpack(">H", data[4:6])[0]
    out = {}
    for i in range(count):
        rec = 12 + 16 * i
        tag = data[rec : rec + 4].decode("latin-1")
        out[tag] = struct.unpack(">I", data[rec + 8 : rec + 12])[0]
    return out


def _format4(data: bytes, base: int) -> set[int]:
    seg_x2 = struct.unpack(">H", data[base + 6 : base + 8])[0]
    segs = seg_x2 // 2
    ends = base + 14
    starts = ends + seg_x2 + 2  # + the reserved pad
    deltas = starts + seg_x2
    ranges = deltas + seg_x2

    covered: set[int] = set()
    for i in range(segs):
        end = struct.unpack(">H", data[ends + 2 * i : ends + 2 * i + 2])[0]
        start = struct.unpack(">H", data[starts + 2 * i : starts + 2 * i + 2])[0]
        delta = struct.unpack(">h", data[deltas + 2 * i : deltas + 2 * i + 2])[0]
        offset = struct.unpack(">H", data[ranges + 2 * i : ranges + 2 * i + 2])[0]
        for code in range(start, min(end, 0xFFFE) + 1):
            if offset == 0:
                glyph = (code + delta) & 0xFFFF
            else:
                at = ranges + 2 * i + offset + 2 * (code - start)
                glyph = struct.unpack(">H", data[at : at + 2])[0]
                if glyph:
                    glyph = (glyph + delta) & 0xFFFF
            if glyph:
                covered.add(code)
    return covered


def _format12(data: bytes, base: int) -> set[int]:
    groups = struct.unpack(">I", data[base + 12 : base + 16])[0]
    covered: set[int] = set()
    for i in range(groups):
        rec = base + 16 + 12 * i
        start, end, glyph = struct.unpack(">III", data[rec : rec + 12])
        if glyph:
            covered.update(range(start, end + 1))
    return covered


def font_coverage(path: Path) -> set[int]:
    """Every code point one font file maps to a real glyph."""
    data = path.read_bytes()
    cmap = _tables(data)["cmap"]
    subtables = struct.unpack(">H", data[cmap + 2 : cmap + 4])[0]
    covered: set[int] = set()
    for i in range(subtables):
        rec = cmap + 4 + 8 * i
        offset = struct.unpack(">I", data[rec + 4 : rec + 8])[0]
        base = cmap + offset
        fmt = struct.unpack(">H", data[base : base + 2])[0]
        if fmt == 4:
            covered |= _format4(data, base)
        elif fmt == 12:
            covered |= _format12(data, base)
    return covered


# ---------------------------------------------------------------------------


def book_sources() -> list[Path]:
    """Every Markdown file that becomes part of a book."""
    out: list[Path] = []
    for book_dir in _BOOK_DIRS:
        out += bg.discover_chapters(book_dir)
        colophon = book_dir / "colophon.md"
        if colophon.exists():
            out.append(colophon)
    return out


class FontCoverageTest(unittest.TestCase):
    def setUp(self):
        self.fonts = sorted(_FONT_DIR.glob("*.ttf"))
        self.assertTrue(self.fonts, "no fonts vendored under docs/shared/fonts")
        self.covered: set[int] = set()
        for font in self.fonts:
            self.covered |= font_coverage(font)

    def test_the_reader_agrees_with_what_the_faces_are_known_to_have(self):
        # A cmap parser that returned the empty set, or every code point, would
        # make the real test below vacuous in either direction.
        self.assertTrue({ord(c) for c in "abcXYZ0189 .,;-—…"} <= self.covered)
        self.assertNotIn(0x21D2, self.covered)  # ⇒, in neither face
        self.assertNotIn(0x1F600, self.covered)

    def test_every_character_in_every_book_can_be_drawn(self):
        drawn = {ord(c) for c in bg._DRAWN_MARKS}
        available = self.covered | drawn | {ord("\n"), ord("\t")}

        missing: dict[str, set[str]] = {}
        for path in book_sources():
            for char in set(path.read_text(encoding="utf-8")):
                if ord(char) not in available:
                    missing.setdefault(char, set()).add(path.name)

        self.assertEqual(
            missing,
            {},
            "no vendored font has these, and the template draws none of them, so "
            "Typst will leave a blank where each one stands: "
            + ", ".join(f"U+{ord(c):04X} {c!r} in {sorted(f)}" for c, f in sorted(missing.items())),
        )

    def test_the_template_names_only_vendored_faces(self):
        # The guarantee above is only worth anything if the template cannot ask
        # for a family that is not in the directory this test measured.
        source = _TEMPLATE.read_text(encoding="utf-8")
        families = set(re.findall(r"^#let (?:body|mono)-font = \((.*)\)$", source, re.M))
        self.assertTrue(families, "template no longer declares its font stacks as tuples")
        named = {
            name.strip().strip('"') for line in families for name in line.split(",") if name.strip()
        }
        stems = {f.name.split("[")[0].split("-")[0] for f in self.fonts}
        self.assertEqual(
            named - stems, set(), f"template names a face not vendored: {named - stems}"
        )

    def test_typst_fallback_is_off(self):
        # With fallback on, a missing glyph is quietly borrowed from the build
        # machine and this whole file proves nothing.
        source = _TEMPLATE.read_text(encoding="utf-8")
        settings = re.findall(r"^\s+fallback: false,$", source, re.M)
        self.assertEqual(len(settings), 2, "one per layout: guide and card")


if __name__ == "__main__":
    unittest.main()
