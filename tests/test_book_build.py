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


def _load_script(name: str):
    path = _REPO_ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registered before exec: @dataclass resolves annotations through
    # sys.modules[cls.__module__], which blows up if the module isn't there.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# The dialect (what the Markdown means) and the Typst renderer (what it becomes).
bd = _load_script("bookdoc")
bg = _load_script("build_book")


def _chapters() -> list[Path]:
    """Every chapter of every book, in reading order."""
    return [p for book_dir in _BOOK_DIRS for p in bd.discover_chapters(book_dir)]


def _load(path: Path):
    """One chapter, with its own book's chapter numbers and anchors in scope.

    Both are checked against the book the chapter appears in, so a chapter
    loaded without them would reject "see Appendix F", or a link at a section
    of the next file, as naming nothing.
    """
    siblings = bd.discover_chapters(path.parent)
    return bd.load_chapter(
        path,
        bg.TypstEmitter(),
        bd.chapter_numbers(siblings),
        bd.section_anchors(siblings),
    )


def convert(
    markdown: str,
    path: Path | None = None,
    chapters: frozenset[str] | None = None,
    anchors: frozenset[str] | None = None,
) -> str:
    """Convert a chapter body, with the `# Title` line already accounted for."""
    conv = bd.Converter(
        path or (_GUIDE_DIR / "99-test.md"),
        1,
        bg.TypstEmitter(),
        chapters or frozenset(),
        anchors or frozenset(),
    )
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

    def test_cross_reference_becomes_a_link_to_the_opener(self):
        out = convert("see Appendix F for the rest", chapters=frozenset({"F"})).strip()
        self.assertEqual(out, 'see #link(label("ch-F"))[Appendix F] for the rest')

    def test_cross_reference_to_a_chapter_the_book_lacks_is_rejected(self):
        with self.assertRaises(bd.BookError) as ctx:
            convert("see Chapter 9", chapters=frozenset({"1"}))
        self.assertIn("does not have", str(ctx.exception))

    def test_a_word_after_the_letter_is_not_a_cross_reference(self):
        # "Appendix Reference" is a title, not a pointer at appendix R.
        out = convert("the Appendix Reference chapter", chapters=frozenset({"R"})).strip()
        self.assertNotIn("#link", out)

    def test_cross_reference_inside_a_code_span_is_left_alone(self):
        out = convert("`Appendix F`", chapters=frozenset({"F"})).strip()
        self.assertEqual(out, "`Appendix F`")

    def test_marks_the_body_face_lacks_are_drawn(self):
        # Bare `#rarrow` would swallow the following word as part of its name,
        # and a following `(` would be read as a call on it.
        out = convert("A ✓ works; low→high; and→(so on)").strip()
        self.assertIn("#[#tick]/**/", out)
        self.assertIn("#[#rarrow]/**/high", out)
        self.assertIn("#[#rarrow]/**/(so on)", out)


class ProseGuardTest(unittest.TestCase):
    def test_double_hyphen_in_prose_is_rejected(self):
        # Typst turns a bare `--` into an en dash, so a flag written outside
        # backticks would render wrong. Fail rather than mangle it.
        with self.assertRaises(bd.BookError) as ctx:
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
        conv = bd.Converter(_GUIDE_DIR / "99-test.md", 1, bg.TypstEmitter())
        conv.convert("# Title\n\n## One\n\n## Two\n\ntext\n")
        self.assertEqual(conv.title, "Title")
        self.assertEqual(
            [(ref.label, title) for ref, title in conv.sections],
            [("sec-99-test-one", "One"), ("sec-99-test-two", "Two")],
        )

    def test_opener_page_sections_carry_their_markup(self):
        # Appendix A's sections are literals. Quoted as strings for the opener
        # page they arrived with their backticks showing.
        conv = bd.Converter(_GUIDE_DIR / "99-test.md", 1, bg.TypstEmitter())
        conv.convert("# Title\n\n## `[hardware]`\n\ntext\n")
        self.assertEqual(
            [(ref.label, title) for ref, title in conv.sections],
            [("sec-99-test-hardware", "`[hardware]`")],
        )
        self.assertEqual(
            bg.typst_content_list(conv.sections),
            '((label: "sec-99-test-hardware", title: [`[hardware]`]),)',
        )

    def test_callout(self):
        out = convert("> [!NOTE]\n> Careful with that.\n")
        self.assertIn('#callout(kind: "NOTE")[', out)
        self.assertIn("Careful with that.", out)

    def test_unknown_callout_kind_is_rejected(self):
        with self.assertRaises(bd.BookError):
            convert("> [!GOTCHA]\n> nope\n")

    def test_bare_blockquote_is_rejected(self):
        with self.assertRaises(bd.BookError):
            convert("> just a quote\n")

    def test_code_block_keeps_its_language(self):
        out = convert("```bash\nuv sync\n```\n")
        self.assertIn("block: true", out)
        self.assertIn('lang: "bash"', out)
        self.assertIn("uv sync", out)

    def test_unterminated_code_fence_is_rejected(self):
        with self.assertRaises(bd.BookError):
            convert("```bash\nuv sync\n")

    def test_table(self):
        out = convert("| A | B |\n|---|--:|\n| 1 | 2 |\n")
        self.assertIn("#table(", out)
        self.assertIn("columns: 2", out)
        self.assertIn("align: (left, right,)", out)

    def test_table_with_a_short_row_is_rejected(self):
        with self.assertRaises(bd.BookError):
            convert("| A | B |\n|---|---|\n| 1 |\n")

    def test_fields_directive_hands_the_table_to_the_template(self):
        # The width of a settings list is a design decision, so the converter
        # names the template's helper instead of choosing columns itself.
        out = convert("<!-- table: fields -->\n| A | B |\n|---|---|\n| 1 | 2 |\n")
        self.assertIn("#fields-table(", out)
        self.assertNotIn("columns:", out)

    def test_fields_directive_requires_two_columns(self):
        with self.assertRaises(bd.BookError) as ctx:
            convert("<!-- table: fields -->\n| A | B | C |\n|---|---|---|\n| 1 | 2 | 3 |\n")
        self.assertIn("2 columns", str(ctx.exception))

    def test_fields_directive_must_be_followed_by_a_table(self):
        with self.assertRaises(bd.BookError):
            convert("<!-- table: fields -->\n\nordinary prose\n")

    def test_unknown_directive_is_rejected(self):
        with self.assertRaises(bd.BookError) as ctx:
            convert("<!-- table: sideways -->\n| A | B |\n|---|---|\n| 1 | 2 |\n")
        self.assertIn("unknown directive", str(ctx.exception))

    def test_index_directive_sets_locators_as_pages(self):
        # The Markdown links a term at the section that discusses it, because
        # a section title is the only locator github.com has. On paper the
        # answer to "where" is a page, and the same link resolves to one.
        anchors = frozenset({"sec-99-test-alpha", "sec-99-test-beta"})
        out = convert(
            "<!-- table: index -->\n| Term | See |\n|---|---|\n"
            "| `dither` | [Alpha (3)](#alpha), [Beta (4)](#beta) |\n",
            anchors=anchors,
        )
        self.assertIn("#fields-table(", out)
        self.assertIn('#pagerefs((label("sec-99-test-alpha"), label("sec-99-test-beta"),))', out)
        self.assertNotIn("Alpha", out)

    def test_index_directive_leaves_the_term_column_alone(self):
        out = convert(
            "<!-- table: index -->\n| Term | See |\n|---|---|\n"
            "| [Alpha (3)](#alpha) | [Alpha (3)](#alpha) |\n",
            anchors=frozenset({"sec-99-test-alpha"}),
        )
        # Column 0 is a term, never a locator, so it stays an ordinary link
        # even when it happens to be shaped like one.
        self.assertIn('#link(label("sec-99-test-alpha"))[Alpha (3)]', out)

    def test_index_directive_falls_back_for_an_ordinary_cell(self):
        out = convert(
            "<!-- table: index -->\n| Term | See |\n|---|---|\n| `dither` | nowhere yet |\n"
        )
        self.assertIn("nowhere yet", out)
        self.assertNotIn("#pagerefs", out)

    def test_a_long_index_term_can_wrap_inside_its_column(self):
        # `hue_corrections_replace_defaults` has no space to break at and ran
        # over the locator column; each underscore now carries a zero-width
        # break opportunity, in the .typ only.
        out = convert(
            "<!-- table: index -->\n| Term | See |\n|---|---|\n"
            "| `hue_corrections_replace_defaults` | [A (1)](#a) |\n",
            anchors=frozenset({"sec-99-test-a"}),
        )
        self.assertIn(f"hue_{bg._ZWSP}corrections_{bg._ZWSP}replace_{bg._ZWSP}defaults", out)

    def test_a_cell_opening_with_a_typst_marker_is_neutralized(self):
        # `+ pairs that flicker mildly` at the head of a cell is not a list.
        out = convert("| A | B |\n|---|---|\n| x | + pairs that flicker mildly |\n")
        self.assertIn("[\\+ pairs that flicker mildly]", out)
        out = convert("| A | B |\n|---|---|\n| x | 3. of a kind |\n")
        self.assertIn("[3\\. of a kind]", out)

    def test_br_becomes_a_line_break(self):
        out = convert("| A | B |\n|---|---|\n| one<br>two | 2 |\n")
        self.assertIn("one#linebreak()two", out)

    def test_lists(self):
        out = convert("- one\n- two\n")
        self.assertIn("- one", out)
        out = convert("1. first\n2. second\n")
        self.assertIn("+ first", out)

    def test_figure_requires_a_caption(self):
        with self.assertRaises(bd.BookError):
            convert("![](img/logo-cover.png)")

    def test_figure_path_is_rewritten_root_relative(self):
        # The book writes `img/x.png`, but the `image()` call it lands in is in
        # docs/shared/template.typ -- and Typst resolves a relative path
        # against the file the call is written in. A book-relative path would
        # be looked for next to the template, where nothing lives.
        out = convert("![A caption.](img/logo-cover.png)").strip()
        self.assertIn('#screenshot("/docs/guide/img/logo-cover.png"', out)

    def test_missing_figure_is_rejected(self):
        with self.assertRaises(bd.BookError) as ctx:
            convert("![A caption.](img/does-not-exist.png)")
        self.assertIn("figure not found", str(ctx.exception))

    def test_inline_image_is_rejected(self):
        with self.assertRaises(bd.BookError):
            convert("text with ![a pic](img/logo-cover.png) inside")

    def test_body_before_the_title_is_rejected(self):
        conv = bd.Converter(_GUIDE_DIR / "99-test.md", 1, bg.TypstEmitter())
        with self.assertRaises(bd.BookError):
            conv.convert("A paragraph before any heading.\n")


class SectionAnchorTest(unittest.TestCase):
    """Section anchors have to be GitHub's, because the Markdown is the book.

    A link written `04-display-pipeline.md#fades` has to resolve on github.com
    *and* land on the right page of the PDF, so the slug rule is GitHub's rule
    and the two are checked against the same source.
    """

    def test_slugs_follow_githubs_rule(self):
        cases = {
            # The em dash is not alphanumeric so it vanishes; the two spaces
            # around it do not, and each becomes a hyphen.
            "Which Pixel Takes Which — `dither`": "which-pixel-takes-which--dither",
            "`big_text` Wants the Scene to Itself": "big_text-wants-the-scene-to-itself",
            "`[hardware]`": "hardware",
            "Overlay and Display-Mode Compatibility": "overlay-and-display-mode-compatibility",
        }
        for heading, slug in cases.items():
            with self.subTest(heading=heading):
                self.assertEqual(bd.heading_slug(heading), slug)

    def test_a_repeated_heading_is_disambiguated(self):
        slugs = bd.file_section_slugs("## Fades\n\n### Fades\n\n## Other\n")
        self.assertEqual(slugs, ["fades", "fades-1", "other"])

    def test_a_heading_inside_a_code_fence_is_not_a_section(self):
        # A `#` comment in a TOML listing is not a heading, and the converter
        # would not emit a label for one either.
        slugs = bd.file_section_slugs("## Real\n\n```toml\n## Not a heading\n```\n")
        self.assertEqual(slugs, ["real"])

    def test_the_label_names_the_file_not_the_chapter_number(self):
        # Keyed on the filename so renumbering a chapter does not silently
        # retarget every link into it -- and so the two `## Generators`
        # sections, in different files, stay apart.
        self.assertEqual(
            bd.section_label("04-display-pipeline", "fades"), "sec-04-display-pipeline-fades"
        )

    def test_subsections_are_labeled_too(self):
        # `###` is the granularity a reader looks things up at: each scene
        # type and each `[color]` setting is one.
        out = convert("## Section\n\n### Sub\n")
        self.assertIn('#label("sec-99-test-section")', out)
        self.assertIn('#label("sec-99-test-sub")', out)

    def test_a_section_link_targets_a_label_not_a_url(self):
        # A string destination is a URL, so a relative `.md#anchor` reached the
        # PDF as a dead link.
        anchors = frozenset({"sec-04-display-pipeline-fades"})
        out = convert("see [Fades](04-display-pipeline.md#fades)", anchors=anchors).strip()
        self.assertEqual(out, 'see #link(label("sec-04-display-pipeline-fades"))[Fades]')

    def test_a_bare_anchor_means_this_same_file(self):
        anchors = frozenset({"sec-99-test-fades"})
        out = convert("see [Fades](#fades)", anchors=anchors).strip()
        self.assertEqual(out, 'see #link(label("sec-99-test-fades"))[Fades]')

    def test_an_unresolvable_anchor_fails_the_build(self):
        anchors = frozenset({"sec-99-test-fades"})
        with self.assertRaises(bd.BookError) as ctx:
            convert("see [Fades](#fadez)", anchors=anchors)
        message = str(ctx.exception)
        self.assertIn("names no section", message)
        self.assertIn("sec-99-test-fades", message)  # the difflib suggestion

    def test_an_ordinary_url_is_untouched(self):
        # Every cross-document link in the books is an absolute GitHub URL, and
        # none of them may start resolving against the anchor set.
        out = convert("see [the repo](https://github.com/x/y#readme)").strip()
        self.assertEqual(out, 'see #link("https://github.com/x/y#readme")[the repo]')

    def test_every_label_in_a_book_is_unique(self):
        # Typst resolves a reference by label, so two sections sharing one
        # would silently send half the links to the wrong page.
        for book_dir in _BOOK_DIRS:
            with self.subTest(book=book_dir.name):
                labels = []
                for path in bd.discover_chapters(book_dir):
                    _, body, _ = bd.parse_front_matter(path.read_text(encoding="utf-8"), path)
                    labels += [bd.section_label(path.stem, s) for s in bd.file_section_slugs(body)]
                duplicates = {label for label in labels if labels.count(label) > 1}
                self.assertEqual(duplicates, set(), f"duplicate section labels: {duplicates}")

    def test_the_pre_pass_finds_exactly_what_the_converter_emits(self):
        # The anchor set is read from the Markdown by regex and the labels are
        # emitted by the converter. Two readings of the same file, and a link
        # that passes the first check and misses the second is a dead link in
        # print -- so they are pinned to each other.
        for book_dir in _BOOK_DIRS:
            with self.subTest(book=book_dir.name):
                paths = bd.discover_chapters(book_dir)
                declared = bd.section_anchors(paths)
                emitted = set()
                for path in paths:
                    body = _load(path).body
                    emitted.update(re.findall(r'#label\("(sec-[^"]+)"\)', body))
                self.assertEqual(emitted, set(declared))


class FrontMatterTest(unittest.TestCase):
    def test_parses_a_number(self):
        fields, body, offset = bd.parse_front_matter(
            "---\nnumber: 2\n---\n# Title\n", _GUIDE_DIR / "99-test.md"
        )
        self.assertEqual(fields, {"number": "2"})
        self.assertEqual(body.strip(), "# Title")
        self.assertEqual(offset, 4)

    def test_absent_front_matter_is_fine(self):
        fields, body, offset = bd.parse_front_matter("# Title\n", _GUIDE_DIR / "99-test.md")
        self.assertEqual(fields, {})
        self.assertEqual(body.strip(), "# Title")
        self.assertEqual(offset, 1)

    def test_unknown_key_is_rejected(self):
        with self.assertRaises(bd.BookError):
            bd.parse_front_matter("---\nauthor: nobody\n---\n# T\n", _GUIDE_DIR / "99-test.md")

    def test_unclosed_front_matter_is_rejected(self):
        with self.assertRaises(bd.BookError):
            bd.parse_front_matter("---\nnumber: 1\n# T\n", _GUIDE_DIR / "99-test.md")


class BookTomlTest(unittest.TestCase):
    """`book.toml` has to carry everything the layout it names will ask for."""

    def _write_book(self, **keys: str) -> Path:
        book_dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        body = "\n".join(f'{key} = "{value}"' for key, value in keys.items())
        (book_dir / "book.toml").write_text(f"[book]\n{body}\n", encoding="utf-8")
        return book_dir

    def test_unknown_layout_is_rejected(self):
        book_dir = self._write_book(layout="pamphlet", output="x")
        with self.assertRaises(bd.BookError) as ctx:
            bd.load_book_toml(book_dir)
        self.assertIn("layout must be one of", str(ctx.exception))

    def test_a_missing_key_names_itself(self):
        # A book that lost its cover logo should fail the build, not render a
        # PDF with a blank front that nobody notices until it is published.
        book_dir = self._write_book(layout="guide", output="x", title="t")
        with self.assertRaises(bd.BookError) as ctx:
            bd.load_book_toml(book_dir)
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
        self.assertIn("#toc()", typst)
        self.assertIn("#chapter(", typst)
        # Applied as show rules, not called. Both contain a `set page`, which
        # in Typst reaches only to the end of the block it is written in — so
        # `#mainmatter()` switched the printed folio (the footer reads a state)
        # and left the PDF's own page labels roman for the whole book.
        self.assertIn("#show: frontmatter", typst)
        self.assertIn("#show: mainmatter", typst)

    def test_a_guide_without_a_colophon_says_so(self):
        # Every other book problem exits with an `error:` line. A missing
        # colophon used to come out as a raw FileNotFoundError traceback,
        # which the guide never hit because it has always had one -- but a
        # second book starts life without it.
        book_dir = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        # Built from LAYOUT_KEYS rather than spelled out, so this stays a test
        # about the colophon. Listing the keys by hand meant that adding a
        # required one (`volume`, for the second book's cover) failed here
        # instead -- on the wrong error, from a test that never mentions it.
        keys = dict.fromkeys(bd.LAYOUT_KEYS["guide"], "x") | {"logo": "logo.png"}
        body = "\n".join(f'{key} = "{value}"' for key, value in keys.items())
        (book_dir / "book.toml").write_text(
            f'[book]\nlayout = "guide"\noutput = "c64cast-book"\n{body}\n',
            encoding="utf-8",
        )
        (book_dir / "logo.png").write_bytes(b"")
        (book_dir / "01-one.md").write_text("---\nnumber: 1\n---\n# One\n\n## Section\n\nText.\n")

        with mock.patch.object(bd, "REPO_ROOT", book_dir):
            with self.assertRaises(bd.BookError) as ctx:
                bg.build(book_dir)
        self.assertIn("colophon.md", str(ctx.exception))

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
        with mock.patch.object(bd, "REPO_ROOT", book_dir):
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
                paths = bd.discover_chapters(book_dir)
                matches = [re.match(r"^(\d+)-", p.name) for p in paths]
                self.assertTrue(all(matches))
                prefixes = [int(m.group(1)) for m in matches if m]
                self.assertEqual(prefixes, sorted(prefixes))
                self.assertEqual(len(prefixes), len(set(prefixes)), "duplicate prefixes")

    def test_every_chapter_has_a_title(self):
        for path in _chapters():
            with self.subTest(chapter=path.name):
                self.assertTrue(_load(path).title)

    def test_numbered_chapters_have_sections(self):
        for path in _chapters():
            chapter = _load(path)
            if chapter.number is not None:
                with self.subTest(chapter=path.name):
                    self.assertTrue(chapter.sections)

    def test_chapter_numbers_are_unique_and_consecutive(self):
        # Per book, and only the digits: lettered numbers are appendices, which
        # the template renders as APPENDIX A rather than CHAPTER A.
        for book_dir in _BOOK_DIRS:
            with self.subTest(book=book_dir.name):
                numbers = [
                    c.number for c in (_load(p) for p in bd.discover_chapters(book_dir)) if c.number
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

    def test_no_listing_is_wider_than_the_page(self):
        # Typst wraps an over-long line in a code block rather than complaining,
        # and a wrapped listing does not look broken -- it looks like a second
        # line of output the program never printed. `--profile`'s sample came
        # out as six lines of four, and a class definition wrapped mid-signature
        # in the middle of Chapter 7. Nothing catches that but a measure.
        #
        # A line whose longest unbreakable run is already wider than the measure
        # is exempt: the schema directive in the User's Guide is a URL that has
        # to be copied verbatim, and no reflowing will shorten it.
        for book_dir in _BOOK_DIRS:
            width = bg.CODE_WIDTH[bd.load_book_toml(book_dir)["layout"]]
            for path in bd.discover_chapters(book_dir):
                _, body, _ = bd.parse_front_matter(path.read_text(encoding="utf-8"), path)
                fenced = False
                for lineno, line in enumerate(body.split("\n"), start=1):
                    if line.strip().startswith("```"):
                        fenced = not fenced
                        continue
                    longest = max((len(tok) for tok in line.split()), default=0)
                    if not fenced or len(line) <= width or longest > width:
                        continue
                    self.fail(
                        f"{path.name}:{lineno}: listing line is {len(line)} characters, "
                        f"and {width} fit on a {book_dir.name} page:\n  {line}"
                    )

    def test_the_makefile_knows_every_book(self):
        # A book's directory and artifact basename are spelled in both its
        # book.toml and the Makefile, which renders it and cleans up after it.
        # The Makefile cannot read the TOML without either a Python it must not
        # need for `clean` or a sed that fails silently, so the two spellings
        # are held together here instead.
        makefile = (_REPO_ROOT / "Makefile").read_text(encoding="utf-8")
        for book_dir in _BOOK_DIRS:
            with self.subTest(book=book_dir.name):
                rel = book_dir.relative_to(_REPO_ROOT).as_posix()
                self.assertIn(rel, makefile, f"the Makefile never names {rel}")
                output = bd.load_book_toml(book_dir)["output"]
                self.assertIn(output, makefile, f"the Makefile cannot render {output}")


if __name__ == "__main__":
    unittest.main()
