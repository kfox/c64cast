"""Tests for the generated reference appendices + the committed files.

Guards the same thing tests/test_schema.py guards for the JSON schema, for the
same reason: the files are generated but committed, so nothing forces them to
be regenerated except a test that notices.

  * every file scripts/gen_reference_appendices.py owns matches a fresh run
    (so `make reference-appendices` was run after a config/CLI/registry
    change), and
  * the text it emits is safe for both renderers -- it survives
    scripts/build_book.py's deliberately small Markdown subset, which rejects
    what it cannot translate rather than dropping it.

The second is the one that catches surprises. Help strings are written for a
terminal, where nothing is markup, so a new `--flag` or a `str | None` type
lands in a table cell as an en dash or an extra column. The book build would
fail on it eventually; failing here says which string and why.

scripts/ is not a package, so both modules are loaded by path.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import unittest
from pathlib import Path

_REPO_ROOT = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load(name: str):
    path = _REPO_ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered before exec: @dataclass resolves annotations through
    # sys.modules[cls.__module__], which blows up if the module isn't there.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gen = _load("gen_reference_appendices")
bb = _load("build_book")


class FreshnessTest(unittest.TestCase):
    def test_committed_files_are_fresh(self):
        stale = []
        for path, build in gen.APPENDICES.items():
            current = path.read_text(encoding="utf-8") if path.exists() else None
            if current != gen.render(build):
                stale.append(str(path.relative_to(_REPO_ROOT)))
        self.assertEqual(
            stale,
            [],
            "generated files are stale — run `make reference-appendices`",
        )

    def test_every_generated_file_says_so(self):
        # The marker is how the generator finds the files it owns, and how a
        # human who opened one in an editor is warned before typing into it.
        for path in gen.APPENDICES:
            with self.subTest(file=path.name):
                fields, _, _ = bb.parse_front_matter(path.read_text(encoding="utf-8"), path)
                self.assertEqual(fields.get("generated"), "true")

    def test_the_appendices_cover_the_reference_book(self):
        # A-H are generated; the introduction, the six chapters and the
        # glossary are not. If a hand-written chapter ever acquires the marker,
        # the next `make reference-appendices` would not touch it and the drift
        # guard above would silently pass on a file nobody generates.
        generated = {p for p in gen.APPENDICES if p.parent == gen.REFERENCE_DIR}
        self.assertEqual(len(generated), 8)
        for path in bb.discover_chapters(gen.REFERENCE_DIR):
            fields, _, _ = bb.parse_front_matter(path.read_text(encoding="utf-8"), path)
            with self.subTest(chapter=path.name):
                self.assertEqual(fields.get("generated") == "true", path in generated)


class ConverterSafetyTest(unittest.TestCase):
    """The generated Markdown has to survive the book converter.

    Every failure mode here is a real one that help text has already produced:
    an unbackticked `--flag` (Typst renders `--` as an en dash, so the
    converter refuses it), and a bare `|` in a type or a quoted choice list
    (which splits the table row and changes the cell count).
    """

    def test_every_generated_file_converts(self):
        for path in gen.APPENDICES:
            with self.subTest(file=path.name):
                chapters = bb.chapter_numbers(bb.discover_chapters(path.parent))
                self.assertTrue(bb.load_chapter(path, chapters).title)

    def test_the_reference_book_builds(self):
        self.assertIn("#show: guide.with(", bb.build(gen.REFERENCE_DIR))


class TextEscapingTest(unittest.TestCase):
    """The helpers that make terminal help text safe as Markdown."""

    def test_a_bare_flag_is_backticked(self):
        self.assertEqual(gen.prose("mute with --no-audio"), "mute with `--no-audio`")

    def test_a_long_flag_paired_with_a_short_one_is_still_found(self):
        # Help text writes the pair as `-u/--url`; an earlier lookbehind
        # excluded `/` and left the long form bare, which the converter
        # rejected as an en dash.
        self.assertEqual(gen.prose("saves -u/--url"), "saves `-u/--url`")

    def test_a_flag_already_in_a_code_span_is_left_alone(self):
        # Some help strings mark up their own; double-wrapping would put
        # literal backticks in the rendered output.
        self.assertEqual(gen.prose("see `--describe`"), "see `--describe`")

    def test_table_cells_escape_pipes(self):
        rows = gen.table(["A", "B"], [["`str | None`", "'cc'|'note'"]])
        self.assertIn(r"`str \| None`", rows[2])
        self.assertIn(r"'cc'\|'note'", rows[2])

    def test_an_escaped_pipe_survives_the_round_trip(self):
        # Escaped on the way out, unescaped by the converter before inline
        # parsing -- so a code span gets a pipe, not a backslash it would print.
        markdown = "\n".join(gen.table(["Field", "Type"], [["`x`", "`str | None`"]]))
        conv = bb.Converter(_REPO_ROOT / "docs" / "reference" / "99-test.md", 1)
        conv.title = "Test"
        typst = conv.convert(markdown)
        self.assertIn("columns: 2", typst)
        self.assertIn("str | None", typst)
        self.assertNotIn("\\|", typst)

    def test_an_empty_table_is_omitted_entirely(self):
        # The converter needs the alignment row to see a table at all, so a
        # header with no body would render as a lone empty box.
        self.assertEqual(gen.table(["A"], []), [])

    def test_an_enormous_default_is_summarised(self):
        # [midi_control].cc_map's default is two dozen mappings and 2,500
        # characters; printed in full it pushes the column off the page.
        self.assertEqual(gen.fmt_default(list(range(500))), "*500 shipped entries*")

    def test_an_ordinary_default_is_printed_verbatim(self):
        self.assertEqual(gen.fmt_default("http://192.168.2.64"), "`'http://192.168.2.64'`")

    def test_first_sentence_survives_an_abbreviation(self):
        text = "Blurs the frame, e.g. to soften dither. Not reactive."
        self.assertEqual(gen.first_sentence(text), "Blurs the frame, e.g. to soften dither.")


if __name__ == "__main__":
    unittest.main()
