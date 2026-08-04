"""The reference guide's diagrams, and the two things about them that drift.

A diagram is drawn once and looked at once, so the failures worth guarding are
the silent ones: the drawing script's copy of the book's palette going stale
against the template, and a committed PNG no longer being what the script
draws. Neither shows up in a build -- the book renders a wrong-colored or
out-of-date figure perfectly happily.

Pixels are deliberately not compared. Pillow's rasteriser is not stable across
versions, so a byte-for-byte drift test fails on an unrelated dependency bump;
what is compared is the geometry the script asks for and the shot list it
writes.

scripts/ is not a package, so the module is loaded by path.
"""

from __future__ import annotations

import importlib.util
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

_REPO_ROOT = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TEMPLATE = _REPO_ROOT / "docs" / "shared" / "template.typ"
_IMG_DIR = _REPO_ROOT / "docs" / "reference" / "img"
_CHAPTERS = sorted((_REPO_ROOT / "docs" / "reference").glob("*.md"))


def _load_diagrams():
    path = _REPO_ROOT / "scripts" / "make_reference_diagrams.py"
    spec = importlib.util.spec_from_file_location("make_reference_diagrams", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


md = _load_diagrams()


class PaletteTest(unittest.TestCase):
    def test_the_palette_still_matches_the_template(self):
        # The script cannot import the Typst template, so it holds its own copy
        # of the four colors the books are set in. A figure drawn in last
        # season's blue looks fine on its own and wrong on the page.
        typ = _TEMPLATE.read_text(encoding="utf-8")
        for name, color in (
            ("accent", md.ACCENT),
            ("accent-pale", md.ACCENT_PALE),
            ("accent-wash", md.ACCENT_WASH),
            ("ink", md.INK),
        ):
            with self.subTest(color=name):
                m = re.search(rf'#let {re.escape(name)} = rgb\("#([0-9A-Fa-f]{{6}})"\)', typ)
                self.assertIsNotNone(m, f"{_TEMPLATE.name} no longer defines {name}")
                assert m is not None
                expected = tuple(int(m.group(1)[i : i + 2], 16) for i in (0, 2, 4))
                self.assertEqual(color, expected)


class FiguresTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.addCleanup(self._tmp.cleanup)

    def test_every_figure_is_committed_at_the_size_it_is_drawn(self):
        # The release renders the books without the project environment, so a
        # figure that only exists when the script is run does not exist.
        for name, (draw, _, _) in md.FIGURES.items():
            with self.subTest(figure=name):
                path = _IMG_DIR / f"{name}.png"
                self.assertTrue(path.exists(), f"{name}.png is not committed")
                with Image.open(path) as committed:
                    self.assertEqual(committed.size, draw().size)

    def test_every_figure_is_referenced_by_a_chapter(self):
        prose = "\n".join(p.read_text(encoding="utf-8") for p in _CHAPTERS)
        for name in md.FIGURES:
            with self.subTest(figure=name):
                self.assertIn(f"img/{name}.png", prose)

    def test_no_committed_figure_is_orphaned(self):
        drawn = {f"{name}.png" for name in md.FIGURES}
        for path in _IMG_DIR.glob("*.png"):
            with self.subTest(figure=path.name):
                self.assertIn(path.name, drawn, "not drawn by the script")

    def test_the_shot_list_is_fresh(self):
        with mock.patch.object(md, "IMG_DIR", Path(self.tmp)):
            md.write_shot_list()
        self.assertEqual(
            (Path(self.tmp) / "README.md").read_text(encoding="utf-8"),
            (_IMG_DIR / "README.md").read_text(encoding="utf-8"),
            "run `make reference-figures` and commit docs/reference/img/README.md",
        )


if __name__ == "__main__":
    unittest.main()
