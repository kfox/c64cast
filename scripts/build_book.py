#!/usr/bin/env python3
"""Render one book's *.md into a single Typst source for its PDF.

A book is a directory under docs/ holding `NN-name.md` chapters and a
`book.toml`; docs/guide/ is one. The Markdown is that book's only source. It
is ordinary GitHub-flavored Markdown -- it renders correctly on github.com
as-is, and nothing about the *look* of the PDF is decided here. This module
only says what each construct becomes in Typst; docs/shared/template.typ owns
the design, and scripts/bookdoc.py owns the reading of the Markdown.

    python scripts/build_book.py --book-dir docs/guide            # write the .typ
    python scripts/build_book.py --book-dir docs/guide --check    # parse only

`make guide` runs this and then `typst compile`.

The supported Markdown subset is deliberately small, and anything outside it
is a hard error rather than a silent drop -- a manual that quietly loses a
paragraph is worse than one that fails to build:

    # Title                    chapter title (exactly one, first line of body)
    ## Section                 blue section heading; also builds the opener page
    ### Subsection             bold subsection
    #### Sub-subsection        bold run-in
    > [!NOTE] / [!TIP] / [!WARNING]   callout box
    <kbd>RETURN</kbd>          keycap chip
    ![Figure 1-1. Cap.](path)  framed figure (alone in its paragraph)
    ```lang ... ```            code block
    | a | b |                  table (GFM, with alignment row)
    <!-- table: fields -->     the table below is a settings list
    <!-- table: index -->      the same, with locators set as page numbers
    a<br>b                     line break inside a table cell
    - / 1.                     lists, nestable by indentation
    **bold** *italic* `code` [text](url)
    [text](04-name.md#anchor)  a link to that section, `#anchor` for this file
    Chapter 4 / Appendix F     a link to that opener page
    ✓ →                        drawn marks (the body face carries neither)

Per-file YAML front matter carries only what Markdown cannot express:

    ---
    number: 1          # omit entirely for front matter (Quick Start, etc.)
    ---                # "A"/"B" for appendices

The chapter's title comes from its `# H1` and the opener page's section list
is derived from its `##` headings, so neither can drift from the prose.

`book.toml` says which layout the book takes and what its artifacts are
called; see LAYOUT_KEYS in bookdoc.py.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# scripts/ is not a package, and this module is loaded by path (by the tests and
# by gen_reference_appendices.py) as often as it is run -- neither of which puts
# its directory on the path. Adding it is what lets the sibling import below
# resolve in every one of the three cases.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from bookdoc import (  # noqa: E402
    LAYOUT_KEYS,
    BookError,
    Converter,
    Emitter,
    ListItem,
    SectionRef,
    book_version,
    chapter_numbers,
    discover_chapters,
    fail,
    load_book_toml,
    load_chapter,
    root_relative,
    section_anchors,
)

TEMPLATE = "/docs/shared/template.typ"

# The layout's chapter renderer. A card has no room for the guide's full-page
# openers, so its "chapters" are drawn as banded headings instead.
CHAPTER_FN = {"guide": "chapter", "card": "card-chapter"}

# How many characters of a listing fit on one line, per layout.
#
# Derived from the template rather than chosen. A guide page is 6.24in with
# 0.82in margins, a code block insets 0.9 x the 10pt body size on each side,
# and Inconsolata advances exactly 0.5em -- so 62. A card is us-letter with
# 0.5in margins in two columns at 8.5pt, which comes to 57.
#
# Nothing enforces it at render time: Typst wraps a long line rather than
# complaining, and a wrapped listing is not obviously wrong on screen -- it
# reads as a second line of output the program never printed. So the guard is
# a test (tests/test_book_build.py), and this is the number it holds books to.
CODE_WIDTH = {"guide": 62, "card": 57}

# Keys naming a file relative to the book directory. They are rewritten to
# root-relative paths on the way out, because Typst resolves a relative path
# against the file the call is written in -- and these are written into the
# shared template, which lives nowhere near the book.
PATH_KEYS = ("logo",)

# Characters that carry meaning in Typst markup and so must be escaped in any
# run of literal prose. `-` and `.` are deliberately absent: Typst turns `--`
# into an en dash and `...` into an ellipsis, which is what we want in prose.
# Command-line flags always live in backticks, where no substitution happens;
# `check_prose` below enforces that.
#
# `/` is here because Typst comments (`//` and `/*`) are live in markup mode
# too: an unescaped URL in prose comments out the rest of its line, taking the
# closing bracket of whatever content block it sits in with it. That surfaced
# as "unclosed delimiter" pointing at a table three lines earlier. `\/` renders
# as an ordinary slash, so prose is unaffected.
_TYPST_SPECIAL = set("\\#$*_`<>@[]~/")

# Marks the body face does not carry, which the template draws instead of
# setting. Written as the ordinary character in the Markdown, so the same file
# still reads correctly on github.com; see `tick` and `rarrow` in the template
# for why they are not simply borrowed from the mono face.
#
# Wrapped and then closed with an empty comment because a mark lands in the
# middle of a word as often as not: bare `#rarrow` swallowed the text after it
# ("low→high" became the variable `rarrowhigh`), the content block stops that,
# and the comment stops a following `(` or `[` from being read as a call on it.
_DRAWN_MARKS = {"✓": "#[#tick]/**/", "→": "#[#rarrow]/**/"}


def escape(text: str) -> str:
    return "".join("\\" + c if c in _TYPST_SPECIAL else c for c in text)


def typst_string(text: str) -> str:
    """Quote a Python string as a Typst string literal."""
    body = text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{body}"'


def typst_inline_raw(code: str) -> str:
    """Emit an inline code span as Typst raw *markup*, not a `#raw(...)` call.

    A call would end in `)`, and Typst reads a following `.` as the start of a
    field access -- so "the file `LICENSE`." came out with a stray gap before
    the full stop. Backtick markup ends in a delimiter that cannot chain.
    Nothing inside is interpreted, so the content needs no escaping; the
    delimiter just has to be longer than any backtick run it contains.
    """
    longest = max((len(m) for m in re.findall(r"`+", code)), default=0)
    fence = "`" * (longest + 1)
    pad = " " if longest else ""
    return f"{fence}{pad}{code}{pad}{fence}"


def check_prose(text: str, path: Path, lineno: int) -> None:
    """Reject prose that Typst's markup shorthands would silently rewrite.

    `--config` written outside backticks would come out as an en dash. Rather
    than mangle it or escape every hyphen, the source has to put it in a code
    span -- which is also how it should read on GitHub.
    """
    if "--" in text:
        fail(
            path,
            lineno,
            "'--' in plain prose becomes an en dash; wrap flags in `backticks`",
        )


class TypstEmitter(Emitter):
    """Every construct as Typst markup. Stateless -- the walker holds the state."""

    # -- inline -------------------------------------------------------------

    def text(self, literal: str) -> str:
        return escape(literal)

    def code(self, body: str) -> str:
        return typst_inline_raw(body)

    def kbd(self, body: str) -> str:
        return f"#kbd[{escape(body)}]"

    def linebreak(self) -> str:
        return "#linebreak()"

    def link(self, href: str, ref: SectionRef | None, body: str) -> str:
        # A section link is spelled at a Typst *label*, not a string: a string
        # destination is a URL, so a relative `.md#anchor` reached the PDF as a
        # dead link. The same Markdown resolves on github.com.
        dest = f"label({typst_string(ref.label)})" if ref else typst_string(href)
        return f"#link({dest})[{body}]"

    def bold(self, body: str) -> str:
        return f"*{body}*"

    def em(self, body: str) -> str:
        return f"_{body}_"

    def xref(self, text: str, number: str) -> str:
        return f"#link(label({typst_string('ch-' + number)}))[{escape(text)}]"

    def mark(self, char: str) -> str:
        return _DRAWN_MARKS[char]

    # -- blocks -------------------------------------------------------------

    def heading(self, level: int, body: str, label: SectionRef | None) -> str:
        # A separate metadata + label rather than a label on the heading
        # itself, which is the pattern the chapter openers already use: proven
        # to attach, and it renders nothing.
        prefix = f'#metadata("sec")#label({typst_string(label.label)})\n' if label else ""
        return f"{prefix}#heading(level: {level})[{body}]\n"

    def figure(self, src: str, target: Path, caption: str) -> str:
        return f"#screenshot({typst_string(root_relative(target))}, [{caption}])\n"

    def code_block(self, body: str, lang: str) -> str:
        lang_arg = f", lang: {typst_string(lang)}" if lang else ""
        return f"#raw({typst_string(body)}, block: true{lang_arg})\n"

    def callout(self, kind: str, body: str) -> str:
        return f'#callout(kind: "{kind}")[\n{body}\n]\n'

    def locators(self, entries: list[tuple[SectionRef, str]]) -> str:
        # The template's `pagerefs` resolves a page from the same label the
        # link already names, so the Markdown can point at a section -- the
        # only locator github.com has -- and the PDF still prints a page. The
        # section's name goes with it and is dropped here: on a page reference
        # the number is the locator, and printing both would say it twice.
        labels = ", ".join(f"label({typst_string(ref.label)})" for ref, _ in entries)
        return f"#pagerefs(({labels},))"

    def table(
        self,
        header: list[str],
        rows: list[list[str]],
        aligns: list[str],
        kind: str | None,
    ) -> str:
        # A fields table gets its widths from the template, which is where every
        # other measurement in the book is decided; an ordinary table lets Typst
        # size its columns to what is in them.
        call = "#fields-table(" if kind else f"#table(\n  columns: {len(header)},"
        parts = [call, f"  align: ({', '.join(aligns)},),"]
        # A real `table.header`, not just a first row: it repeats when a table
        # splits across a page, and it stops Typst from stranding the header at
        # the foot of one page with its body at the top of the next.
        parts.append("  table.header(" + ", ".join(f"[{c}]" for c in header) + "),")
        for row in rows:
            parts.append("  " + ", ".join(f"[{c}]" for c in row) + ",")
        parts.append(")\n")
        return "\n".join(parts)

    def list_block(self, items: list[ListItem]) -> str:
        """Preserve indentation so Typst reproduces the nesting."""
        lines = []
        for item in items:
            if item.continuation:
                lines.append("  " + item.text)
            else:
                marker = "+" if item.ordered else "-"
                lines.append(f"{item.indent}{marker} {item.text}")
        return "\n".join(lines) + "\n"

    def paragraph(self, body: str) -> str:
        return body + "\n"

    # -- checks -------------------------------------------------------------

    def check_prose(self, literal: str, path: Path, lineno: int) -> None:
        check_prose(literal, path, lineno)


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def out_path(book_dir: Path) -> Path:
    """Where `build()`'s Typst goes: the basename the book gives itself.

    In the book rather than derived from its directory, because the file a
    reader downloads is called `c64cast-users-guide.pdf`, not `guide.pdf`.
    """
    return book_dir / (load_book_toml(book_dir)["output"] + ".typ")


def typst_content_list(items: list[tuple[SectionRef, str]]) -> str:
    """A section list for an opener page: the anchor each entry links at, and
    its title as Typst *content*.

    The title is content and not a string because quoting it would print
    whatever markup it carries -- which is how the appendices' opener pages
    came to list ``` `[hardware]` ``` with the backticks showing.

    The `link()` call is left to the template rather than built here: the
    opener page is blue, the document-wide show rule paints links in accent
    blue, and the fill that resolves that is a design decision. This module
    only says which section each line means.
    """
    entries = [f"(label: {typst_string(ref.label)}, title: [{title}])" for ref, title in items]
    return "(" + ", ".join(entries) + ("," if entries else "") + ")"


def layout_call(book_dir: Path, book: dict[str, str]) -> list[str]:
    """The `#show: <layout>.with(...)` line and its arguments."""
    layout = book["layout"]
    lines = [f"#show: {layout}.with("]
    for key in LAYOUT_KEYS[layout]:
        value = book[key]
        if key in PATH_KEYS:
            target = book_dir / value
            if not target.exists():
                raise BookError(f"{book_dir / 'book.toml'}: {key} not found: {value}")
            value = root_relative(target)
        lines.append(f"  {key.replace('_', '-')}: {typst_string(value)},")
    # Not in book.toml: the version is not book-level metadata somebody edits,
    # it is whatever release this build documents.
    lines.append(f"  version: {typst_string(book_version())},")
    lines.append(")")
    return lines


def build(book_dir: Path) -> str:
    book = load_book_toml(book_dir)
    layout = book["layout"]
    emitter = TypstEmitter()

    paths = discover_chapters(book_dir)
    numbers = chapter_numbers(paths)
    anchors = section_anchors(paths)
    chapters = [load_chapter(p, emitter, numbers, anchors) for p in paths]

    source = root_relative(book_dir).lstrip("/")
    out: list[str] = [
        "// GENERATED by scripts/build_book.py -- do not edit.",
        f"// Source: {source}/*.md   Design: {TEMPLATE.lstrip('/')}",
        "",
        f'#import "{TEMPLATE}": *',
        "",
        *layout_call(book_dir, book),
        "",
    ]

    # Only the bound book gets front matter. A card is a hand-out: it opens on
    # its first line, and a contents page for two pages would be a joke.
    if layout == "guide":
        if not any(c.number is not None for c in chapters):
            raise BookError(f"{book_dir} has no numbered chapters")
        colophon_path = book_dir / "colophon.md"
        try:
            colophon = colophon_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise BookError(f"cannot read {colophon_path}: {exc}") from exc
        colophon_conv = Converter(colophon_path, 1, emitter, numbers, anchors)
        colophon_conv.title = ""  # the colophon is bare prose, no heading
        # `#show:` and not `#frontmatter()`: both switches contain a `set page`,
        # which in Typst reaches only to the end of the block it is written in.
        # Called, they changed the folio (the footer reads a state) and left the
        # PDF's own page labels on the document-level roman for all 205 pages.
        out += [
            f"#colophon[\n{colophon_conv.convert(colophon)}\n]",
            "",
            "#show: frontmatter",
            "",
            "#toc()",
            "",
        ]

    seen_numbered = False
    for chapter in chapters:
        if layout == "guide" and chapter.number is not None and not seen_numbered:
            out += ["#show: mainmatter", ""]
            seen_numbered = True
        number_arg = "none" if chapter.number is None else typst_string(chapter.number)
        out.append(
            f"#{CHAPTER_FN[layout]}(\n"
            f"  number: {number_arg},\n"
            f"  title: {typst_string(chapter.title)},\n"
            f"  contents: {typst_content_list(chapter.sections)},\n"
            f")"
        )
        out += ["", chapter.body, ""]

    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--book-dir",
        type=Path,
        required=True,
        help="the book to render, e.g. docs/guide",
    )
    ap.add_argument("--out", type=Path, help="output .typ path (default: from book.toml)")
    ap.add_argument("--check", action="store_true", help="parse only; write nothing")
    args = ap.parse_args()

    try:
        typst = build(args.book_dir)
        out = args.out or out_path(args.book_dir)
    except BookError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.check:
        print(f"{args.book_dir} source OK")
        return 0

    out.write_text(typst, encoding="utf-8")
    print(f"wrote {out} ({len(typst.splitlines())} lines)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
