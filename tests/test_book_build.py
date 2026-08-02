"""Tests for the book Markdown -> Typst converter.

Guards two separate things:

  * the converter translates each supported construct correctly, and refuses
    anything outside the documented subset instead of silently dropping it
    (a manual that quietly loses a paragraph is worse than one that fails to
    build), and
  * every book's own sources under docs/ still satisfy those rules -- every
    figure it references exists, every chapter has a title, and the whole
    book converts without error.

The book-agnostic checks run against each book found under docs/, so a book
added later is covered the day it lands.

scripts/ is not a package, so the module is loaded by path.
"""

from __future__ import annotations

import importlib.util
import os
import re
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

_REPO_ROOT = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_DOCS = _REPO_ROOT / "docs"
_GUIDE_DIR = _DOCS / "guide"
_SHARED_DIR = _DOCS / "shared"
_BOOK_DIRS = sorted(p.parent for p in _DOCS.glob("*/book.toml"))


def _load_build_book():
    path = _REPO_ROOT / "scripts" / "build_book.py"
    spec = importlib.util.spec_from_file_location("build_book", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered before exec: @dataclass resolves annotations through
    # sys.modules[cls.__module__], which blows up if the module isn't there.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


bg = _load_build_book()


def _chapters() -> list[Path]:
    """Every chapter of every book, in reading order."""
    return [p for book_dir in _BOOK_DIRS for p in bg.discover_chapters(book_dir)]


def convert(markdown: str, path: Path | None = None) -> str:
    """Convert a chapter body, with the `# Title` line already accounted for."""
    conv = bg.Converter(path or (_GUIDE_DIR / "99-test.md"), 1)
    conv.title = "Test"
    return conv.convert(markdown)


class InlineConversionTest(unittest.TestCase):
    def test_bold_and_italic_become_typst_emphasis(self):
        self.assertEqual(convert("**loud** and *soft*").strip(), "*loud* and _soft_")

    def test_underscore_italic(self):
        self.assertEqual(convert("_soft_").strip(), "_soft_")

    def test_backslash_escape_yields_a_literal(self):
        self.assertEqual(convert(r"Jost\*").strip(), r"Jost\*")

    def test_backslash_escape_does_not_open_emphasis(self):
        # Without escape handling the lone `*` pairs with the next one and
        # italicises everything between, silently swallowing the prose.
        out = convert(r"Jost\* and *real* emphasis").strip()
        self.assertEqual(out, r"Jost\* and _real_ emphasis")

    def test_inline_code_uses_raw_markup_not_a_call(self):
        # A `#raw(...)` call ends in `)`, and Typst reads a following `.` as
        # the start of a field access -- which put a stray gap before the full
        # stop in "the file `LICENSE`." Backtick markup cannot chain.
        out = convert("the file `LICENSE`.").strip()
        self.assertEqual(out, "the file `LICENSE`.")
        self.assertNotIn("#raw(", out)

    def test_inline_code_containing_backticks_gets_a_longer_fence(self):
        out = bg.typst_inline_raw("a ` b")
        self.assertTrue(out.startswith("`` ") and out.endswith(" ``"))
        self.assertIn("a ` b", out)

    def test_keycap(self):
        self.assertEqual(convert("Press <kbd>RETURN</kbd>").strip(), "Press #kbd[RETURN]")

    def test_link(self):
        out = convert("see [the docs](https://example.com/a)").strip()
        self.assertEqual(out, 'see #link("https://example.com/a")[the docs]')

    def test_typst_special_characters_are_escaped(self):
        out = convert("costs $5 #1 <tag> [x] ~y @z").strip()
        for fragment in ("\\$", "\\#", "\\<", "\\[", "\\~", "\\@"):
            self.assertIn(fragment, out)

    def test_markup_inside_code_spans_is_not_interpreted(self):
        out = convert("`**not bold**`").strip()
        self.assertEqual(out, "`**not bold**`")


class ProseGuardTest(unittest.TestCase):
    def test_double_hyphen_in_prose_is_rejected(self):
        # Typst turns a bare `--` into an en dash, so a flag written outside
        # backticks would render wrong. Fail rather than mangle it.
        with self.assertRaises(bg.BookError) as ctx:
            convert("pass --config to select a file")
        self.assertIn("en dash", str(ctx.exception))

    def test_double_hyphen_inside_code_is_fine(self):
        self.assertEqual(convert("pass `--config` please").strip(), "pass `--config` please")


class BlockConversionTest(unittest.TestCase):
    def test_headings(self):
        out = convert("## Section\n\n### Sub\n")
        self.assertIn("#heading(level: 2)[Section]", out)
        self.assertIn("#heading(level: 3)[Sub]", out)

    def test_section_headings_are_collected_for_the_opener_page(self):
        conv = bg.Converter(_GUIDE_DIR / "99-test.md", 1)
        conv.convert("# Title\n\n## One\n\n## Two\n\ntext\n")
        self.assertEqual(conv.title, "Title")
        self.assertEqual(conv.sections, ["One", "Two"])

    def test_callout(self):
        out = convert("> [!NOTE]\n> Careful with that.\n")
        self.assertIn('#callout(kind: "NOTE")[', out)
        self.assertIn("Careful with that.", out)

    def test_unknown_callout_kind_is_rejected(self):
        with self.assertRaises(bg.BookError):
            convert("> [!GOTCHA]\n> nope\n")

    def test_bare_blockquote_is_rejected(self):
        with self.assertRaises(bg.BookError):
            convert("> just a quote\n")

    def test_code_block_keeps_its_language(self):
        out = convert("```bash\nuv sync\n```\n")
        self.assertIn("block: true", out)
        self.assertIn('lang: "bash"', out)
        self.assertIn("uv sync", out)

    def test_unterminated_code_fence_is_rejected(self):
        with self.assertRaises(bg.BookError):
            convert("```bash\nuv sync\n")

    def test_table(self):
        out = convert("| A | B |\n|---|--:|\n| 1 | 2 |\n")
        self.assertIn("#table(", out)
        self.assertIn("columns: 2", out)
        self.assertIn("align: (left, right,)", out)

    def test_table_with_a_short_row_is_rejected(self):
        with self.assertRaises(bg.BookError):
            convert("| A | B |\n|---|---|\n| 1 |\n")

    def test_lists(self):
        out = convert("- one\n- two\n")
        self.assertIn("- one", out)
        out = convert("1. first\n2. second\n")
        self.assertIn("+ first", out)

    def test_figure_requires_a_caption(self):
        with self.assertRaises(bg.BookError):
            convert("![](img/logo-cover.png)")

    def test_figure_path_is_rewritten_root_relative(self):
        # The book writes `img/x.png`, but the `image()` call it lands in is in
        # docs/shared/template.typ -- and Typst resolves a relative path
        # against the file the call is written in. A book-relative path would
        # be looked for next to the template, where nothing lives.
        out = convert("![A caption.](img/logo-cover.png)").strip()
        self.assertIn('#screenshot("/docs/guide/img/logo-cover.png"', out)

    def test_missing_figure_is_rejected(self):
        with self.assertRaises(bg.BookError) as ctx:
            convert("![A caption.](img/does-not-exist.png)")
        self.assertIn("figure not found", str(ctx.exception))

    def test_inline_image_is_rejected(self):
        with self.assertRaises(bg.BookError):
            convert("text with ![a pic](img/logo-cover.png) inside")

    def test_body_before_the_title_is_rejected(self):
        conv = bg.Converter(_GUIDE_DIR / "99-test.md", 1)
        with self.assertRaises(bg.BookError):
            conv.convert("A paragraph before any heading.\n")


class FrontMatterTest(unittest.TestCase):
    def test_parses_a_number(self):
        fields, body, offset = bg.parse_front_matter(
            "---\nnumber: 2\n---\n# Title\n", _GUIDE_DIR / "99-test.md"
        )
        self.assertEqual(fields, {"number": "2"})
        self.assertEqual(body.strip(), "# Title")
        self.assertEqual(offset, 4)

    def test_absent_front_matter_is_fine(self):
        fields, body, offset = bg.parse_front_matter("# Title\n", _GUIDE_DIR / "99-test.md")
        self.assertEqual(fields, {})
        self.assertEqual(body.strip(), "# Title")
        self.assertEqual(offset, 1)

    def test_unknown_key_is_rejected(self):
        with self.assertRaises(bg.BookError):
            bg.parse_front_matter("---\nauthor: nobody\n---\n# T\n", _GUIDE_DIR / "99-test.md")

    def test_unclosed_front_matter_is_rejected(self):
        with self.assertRaises(bg.BookError):
            bg.parse_front_matter("---\nnumber: 1\n# T\n", _GUIDE_DIR / "99-test.md")


class BookTomlTest(unittest.TestCase):
    """`book.toml` has to carry everything the layout it names will ask for."""

    def _write_book(self, **keys: str) -> Path:
        book_dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        body = "\n".join(f'{key} = "{value}"' for key, value in keys.items())
        (book_dir / "book.toml").write_text(f"[book]\n{body}\n", encoding="utf-8")
        return book_dir

    def test_unknown_layout_is_rejected(self):
        book_dir = self._write_book(layout="pamphlet", output="x")
        with self.assertRaises(bg.BookError) as ctx:
            bg.load_book_toml(book_dir)
        self.assertIn("layout must be one of", str(ctx.exception))

    def test_a_missing_key_names_itself(self):
        # A book that lost its cover logo should fail the build, not render a
        # PDF with a blank front that nobody notices until it is published.
        book_dir = self._write_book(layout="guide", output="x", title="t")
        with self.assertRaises(bg.BookError) as ctx:
            bg.load_book_toml(book_dir)
        self.assertIn("logo", str(ctx.exception))

    def test_the_output_basename_names_the_typ(self):
        book_dir = self._write_book(
            layout="card", output="c64cast-card", title="t", subtitle="s", pdf_title="p"
        )
        self.assertEqual(bg.out_path(book_dir).name, "c64cast-card.typ")


class LayoutTest(unittest.TestCase):
    """Each layout gets its own template entry point and its own furniture."""

    def test_the_guide_layout_gets_front_matter(self):
        typst = bg.build(_GUIDE_DIR)
        self.assertIn("#show: guide.with(", typst)
        self.assertIn("#colophon[", typst)
        self.assertIn("#frontmatter()", typst)
        self.assertIn("#toc()", typst)
        self.assertIn("#mainmatter()", typst)
        self.assertIn("#chapter(", typst)

    def test_the_card_layout_gets_none_of_it(self):
        book_dir = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        (book_dir / "book.toml").write_text(
            textwrap.dedent("""\
                [book]
                layout = "card"
                output = "c64cast-card"
                title = "c64cast"
                subtitle = "Performance Card"
                pdf_title = "c64cast Performance Card"
            """),
            encoding="utf-8",
        )
        (book_dir / "01-targets.md").write_text("# Live targets\n\n## Transport\n\nText.\n")

        # Paths are spelled relative to the repo root, so a scratch book has to
        # stand in as one.
        with mock.patch.object(bg, "REPO_ROOT", book_dir):
            typst = bg.build(book_dir)

        self.assertIn("#show: card.with(", typst)
        self.assertIn("#card-chapter(", typst)
        # A card opens on its first line: a contents page for two pages would
        # be a joke, and there is no colophon to put a copyright on.
        for furniture in ("#colophon[", "#frontmatter()", "#toc()", "#mainmatter()"):
            self.assertNotIn(furniture, typst)

    def test_every_book_imports_the_shared_template(self):
        for book_dir in _BOOK_DIRS:
            with self.subTest(book=book_dir.name):
                self.assertIn('#import "/docs/shared/template.typ"', bg.build(book_dir))

    def test_the_shared_template_is_present(self):
        self.assertTrue((_SHARED_DIR / "template.typ").is_file())


class BookSourcesTest(unittest.TestCase):
    """The real content under docs/, not synthetic fixtures."""

    def test_a_book_was_found(self):
        self.assertTrue(_BOOK_DIRS, "no docs/*/book.toml — did a book move?")

    def test_chapters_are_discovered_in_order(self):
        for book_dir in _BOOK_DIRS:
            with self.subTest(book=book_dir.name):
                paths = bg.discover_chapters(book_dir)
                matches = [re.match(r"^(\d+)-", p.name) for p in paths]
                self.assertTrue(all(matches))
                prefixes = [int(m.group(1)) for m in matches if m]
                self.assertEqual(prefixes, sorted(prefixes))
                self.assertEqual(len(prefixes), len(set(prefixes)), "duplicate prefixes")

    def test_every_chapter_has_a_title(self):
        for path in _chapters():
            with self.subTest(chapter=path.name):
                self.assertTrue(bg.load_chapter(path).title)

    def test_numbered_chapters_have_sections(self):
        for path in _chapters():
            chapter = bg.load_chapter(path)
            if chapter.number is not None:
                with self.subTest(chapter=path.name):
                    self.assertTrue(chapter.sections)

    def test_chapter_numbers_are_unique_and_consecutive(self):
        # Per book, and only the digits: lettered numbers are appendices, which
        # the template renders as APPENDIX A rather than CHAPTER A.
        for book_dir in _BOOK_DIRS:
            with self.subTest(book=book_dir.name):
                numbers = [
                    c.number
                    for c in (bg.load_chapter(p) for p in bg.discover_chapters(book_dir))
                    if c.number
                ]
                self.assertEqual(len(numbers), len(set(numbers)))
                digits = [n for n in numbers if n.isdigit()]
                self.assertEqual(digits, [str(i) for i in range(1, len(digits) + 1)])

    def test_every_referenced_figure_exists(self):
        # load_chapter() raises on a missing figure, so reaching the assert
        # means they all resolved; the count guards against a book quietly
        # losing its illustrations.
        found = 0
        for path in _chapters():
            text = path.read_text(encoding="utf-8")
            for src in re.findall(r"^!\[[^\]]*\]\(([^)]+)\)$", text, re.MULTILINE):
                self.assertTrue((path.parent / src).exists(), f"{path.name}: {src}")
                found += 1
        self.assertGreater(found, 0)

    def test_every_book_builds(self):
        for book_dir in _BOOK_DIRS:
            with self.subTest(book=book_dir.name):
                self.assertTrue(bg.build(book_dir))


if __name__ == "__main__":
    unittest.main()
