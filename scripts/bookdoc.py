#!/usr/bin/env python3
"""The books' Markdown dialect: what it recognizes, and what it checks.

Two things render a book. `scripts/build_book.py` sets it in Typst for the PDF;
`scripts/build_site.py` renders it as HTML for the documentation site. Both read
the same `docs/<book>/*.md`, and a reader who follows a cross-reference in one
and then the other has to land in the same place -- so the recognition, the
anchor rules and every check live here, once, and each builder supplies only an
`Emitter` saying what its own output looks like.

The supported subset is documented for authors in `docs/guide/README.md` and
listed in `build_book.py`'s docstring. Anything outside it is a hard error
rather than a silent drop: a manual that quietly loses a paragraph is worse than
one that fails to build.

Stdlib only, and no import of `c64cast`. The release renders the books with
`uv run --no-project python`, which has neither the project environment nor the
package on its path.
"""

from __future__ import annotations

import difflib
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import NoReturn, Protocol

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"

CALLOUT_KINDS = ("NOTE", "TIP", "WARNING", "IMPORTANT", "CAUTION")

# What each layout takes from `book.toml`, in the order the Typst template
# declares the parameters. A key is required if it is listed: a book that forgot
# its cover logo should fail here rather than render a coverless PDF.
#
# `output` (the artifact basename) and `layout` itself are consumed by the
# builder and never passed on; `version` is not book metadata somebody edits, so
# it is appended by the build rather than read from the file.
LAYOUT_KEYS = {
    "guide": ("title", "volume", "subtitle", "tagline", "logo", "pdf_title"),
    "card": ("title", "subtitle", "pdf_title"),
}


class BookError(Exception):
    """A problem in a book's source. Always fatal -- never render partial prose."""


def fail(path: Path, lineno: int, message: str) -> NoReturn:
    raise BookError(f"{path.relative_to(REPO_ROOT)}:{lineno}: {message}")


def root_relative(target: Path) -> str:
    """Spell a file the way Typst reads it from anywhere: `/docs/guide/img/x.png`.

    Typst resolves a relative path against the source file the call is written
    in, and every image call is written in docs/shared/template.typ -- so a
    book-relative `img/x.png` would be looked for next to the *template*.
    `typst compile --root .` makes a leading slash mean the repo root, which is
    the one anchor both the template and every book agree on.
    """
    try:
        return "/" + target.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        raise BookError(f"{target} is outside the repository") from None


# ---------------------------------------------------------------------------
# Front matter
# ---------------------------------------------------------------------------


def parse_front_matter(text: str, path: Path) -> tuple[dict[str, str], str, int]:
    """Split leading `---` YAML front matter from the body.

    Only `key: value` scalars are supported -- that is the whole vocabulary a
    book needs, and a full YAML parser would be a dependency for nothing.
    Returns (fields, body, body_start_lineno).
    """
    if not text.startswith("---\n"):
        return {}, text, 1

    lines = text.split("\n")
    try:
        end = lines.index("---", 1)
    except ValueError:
        fail(path, 1, "front matter opened with --- but never closed")

    fields: dict[str, str] = {}
    for i, line in enumerate(lines[1:end], start=2):
        if not line.strip():
            continue
        if ":" not in line:
            fail(path, i, f"front matter line is not `key: value`: {line!r}")
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip().strip("\"'")

    # `generated` marks a chapter written by scripts/gen_reference_appendices.py.
    # The converter does nothing with it: it is there so the drift check can
    # discover its own outputs, and so a human editing one has been warned.
    allowed = {"number", "generated"}
    for key in fields:
        if key not in allowed:
            fail(path, 1, f"unknown front matter key {key!r} (allowed: {sorted(allowed)})")
    return fields, "\n".join(lines[end + 1 :]), end + 2


# ---------------------------------------------------------------------------
# Anchors
# ---------------------------------------------------------------------------

# A Markdown link at a section: `04-display-pipeline.md#anchor`, or bare
# `#anchor` for one in the same file. Anything else -- an absolute URL, a link
# at a whole file -- is left to the ordinary link branch.
_SECTION_HREF_RE = re.compile(r"^(?P<file>\d+-[\w.-]+\.md)?#(?P<slug>[\w-]+)$")


def heading_slug(text: str) -> str:
    """GitHub's heading anchor for one heading's Markdown source.

    Deliberately GitHub's rule and not one of our own: the Markdown *is* the
    book, and a link written `04-display-pipeline.md#which-pixel-takes-which--dither`
    has to work on github.com as well as in the PDF and on the site. Lowercase,
    drop every character that is not alphanumeric, space, hyphen or underscore,
    then spaces to hyphens -- which is why an em dash surrounded by spaces
    leaves two hyphens behind, and why ``## `[hardware]` `` is simply `hardware`.
    """
    slug = text.strip().lower()
    slug = "".join(c for c in slug if c.isalnum() or c in " -_")
    return slug.replace(" ", "-")


def section_label(stem: str, slug: str) -> str:
    """The Typst label a section anchor becomes: `sec-04-display-pipeline-fades`.

    Keyed on the *filename* rather than the chapter number, so renumbering a
    chapter does not silently retarget every link into it -- and so the label
    is a pure function of the Markdown link that names it. It also keeps the
    two `## Generators` sections, in different files, apart.
    """
    return f"sec-{stem}-{slug}"


@dataclass(frozen=True)
class SectionRef:
    """A resolved `file.md#anchor` link, spelled for whichever output wants it.

    Both destinations are derived here rather than in an emitter so that the
    Typst label and the HTML fragment can never name different sections.
    """

    stem: str
    slug: str

    @property
    def label(self) -> str:
        return section_label(self.stem, self.slug)


def file_section_slugs(text: str) -> list[str]:
    """Every `##`/`###` anchor in one file's Markdown, in order, deduped.

    GitHub disambiguates a repeated heading by suffixing `-1`, `-2`, and so
    must we, or the second `### Fades` in a file would take the first one's
    link.
    """
    seen: dict[str, int] = {}
    slugs: list[str] = []
    fenced = False
    for line in text.split("\n"):
        if _FENCE_RE.match(line) or line.strip().startswith("```"):
            fenced = not fenced
            continue
        m = _HEADING_RE.match(line)
        if fenced or not m or len(m.group("hashes")) not in (2, 3):
            # A `#` comment in a fenced TOML listing is not a section, and the
            # converter would not emit a label for one either.
            continue
        slug = heading_slug(m.group("text"))
        count = seen.get(slug, 0)
        seen[slug] = count + 1
        slugs.append(slug if count == 0 else f"{slug}-{count}")
    return slugs


def resolve_section_href(
    href: str,
    path: Path,
    lineno: int,
    anchors: frozenset[str],
) -> SectionRef | None:
    """The section a `file.md#anchor` link targets, or None if it is not one.

    An empty file part means this same file, exactly as on github.com. An
    anchor that resolves nowhere is a hard error rather than a link into
    nothing -- the same bargain the chapter cross-reference makes, and the one
    that catches a section renamed without its links.
    """
    m = _SECTION_HREF_RE.match(href)
    if not m:
        return None
    stem = Path(m.group("file")).stem if m.group("file") else path.stem
    ref = SectionRef(stem, m.group("slug"))
    if ref.label not in anchors:
        near = difflib.get_close_matches(ref.label, sorted(anchors), n=3)
        hint = f"; did you mean {', '.join(near)}?" if near else ""
        fail(path, lineno, f"{href!r} names no section in this book{hint}")
    return ref


# ---------------------------------------------------------------------------
# The emitter interface
# ---------------------------------------------------------------------------


@dataclass
class ListItem:
    """One line of a list block, with the indentation that gave it its depth."""

    indent: str
    ordered: bool
    text: str
    # A wrapped continuation of the item above rather than an item of its own.
    continuation: bool = False


class Emitter(Protocol):
    """What one output format does with each construct the walker recognizes.

    Every method receives text that has already been converted -- a table cell
    arrives as emitted markup, not as Markdown -- so an emitter never re-parses
    and the two outputs cannot disagree about what a construct *is*, only about
    what it looks like.
    """

    # -- inline -------------------------------------------------------------
    def text(self, literal: str) -> str: ...
    def code(self, body: str) -> str: ...
    def kbd(self, body: str) -> str: ...
    def linebreak(self) -> str: ...
    def link(self, href: str, ref: SectionRef | None, body: str) -> str: ...
    def bold(self, body: str) -> str: ...
    def em(self, body: str) -> str: ...
    def xref(self, text: str, number: str) -> str: ...
    def mark(self, char: str) -> str: ...

    # -- blocks -------------------------------------------------------------
    def heading(self, level: int, body: str, label: SectionRef | None) -> str: ...
    def figure(self, src: str, target: Path, caption: str) -> str: ...
    def code_block(self, body: str, lang: str) -> str: ...
    def callout(self, kind: str, body: str) -> str: ...
    def table(
        self,
        header: list[str],
        rows: list[list[str]],
        aligns: list[str],
        kind: str | None,
    ) -> str: ...
    def locators(self, entries: list[tuple[SectionRef, str]]) -> str: ...
    def list_block(self, items: list[ListItem]) -> str: ...
    def paragraph(self, body: str) -> str: ...

    # -- checks -------------------------------------------------------------
    def check_prose(self, literal: str, path: Path, lineno: int) -> None:
        """Reject prose this output would silently rewrite. May be a no-op."""
        ...


# ---------------------------------------------------------------------------
# Inline conversion
# ---------------------------------------------------------------------------

# Ordered: the first pattern to match at a position wins. Code spans come first
# so that markup inside them is never interpreted.
_INLINE = re.compile(
    r"""
      \\(?P<esc>[\\`*_\[\]()#+\-.!<>~{}])
    | (?P<code>`{1,3})(?P<code_body>.+?)(?P=code)
    | <kbd>(?P<kbd>.+?)</kbd>
    | <br\s*/?>(?P<br>)
    | !\[(?P<img_alt>[^\]]*)\]\((?P<img_src>[^)]+)\)
    | \[(?P<link_text>[^\]]+)\]\((?P<link_href>[^)]+)\)
    | \*\*(?P<bold>.+?)\*\*
    | (?<![A-Za-z0-9])_(?P<em_us>[^_]+)_(?![A-Za-z0-9])
    | \*(?P<em_star>[^*]+)\*
    | (?P<xref>(?:Chapter|Appendix)\ (?P<xref_num>[0-9]+|[A-Z]))(?![A-Za-z0-9])
    | (?P<mark>[✓→])
    """,
    re.VERBOSE | re.DOTALL,
)


def convert_inline(
    text: str,
    emitter: Emitter,
    path: Path,
    lineno: int,
    chapters: frozenset[str] = frozenset(),
    anchors: frozenset[str] = frozenset(),
) -> str:
    """Translate one run of Markdown inline markup.

    `chapters` is the set of chapter numbers this book has, which is what makes
    "see Appendix F" a link rather than three words. It is threaded in rather
    than looked up because the answer is per book, and a reference to a chapter
    the book does not have is a hard error -- that is the check that catches a
    renumbering the prose was not told about. `anchors` is the same bargain one
    level down: every section label the book defines, so a link at a section
    can be checked before it reaches the output as a dead destination.
    """
    out: list[str] = []
    pos = 0

    def recurse(inner: str) -> str:
        return convert_inline(inner, emitter, path, lineno, chapters, anchors)

    for m in _INLINE.finditer(text):
        literal = text[pos : m.start()]
        emitter.check_prose(literal, path, lineno)
        out.append(emitter.text(literal))

        if m.group("esc") is not None:
            # A CommonMark backslash escape. This alternative is FIRST in the
            # pattern so that `Jost\*` yields a literal asterisk instead of
            # leaving a stray backslash and opening an emphasis run that eats
            # prose until the next `*` several sentences later.
            out.append(emitter.text(m.group("esc")))
        elif m.group("code") is not None:
            out.append(emitter.code(m.group("code_body")))
        elif m.group("kbd") is not None:
            out.append(emitter.kbd(m.group("kbd")))
        elif m.group("br") is not None:
            # GFM's only way to break a line inside a table cell, which the
            # generated field tables stack a name, a type and a default with.
            out.append(emitter.linebreak())
        elif m.group("img_src") is not None:
            fail(path, lineno, "images must stand alone in their own paragraph")
        elif m.group("link_text") is not None:
            href = m.group("link_href")
            ref = resolve_section_href(href, path, lineno, anchors)
            out.append(emitter.link(href, ref, recurse(m.group("link_text"))))
        elif m.group("bold") is not None:
            out.append(emitter.bold(recurse(m.group("bold"))))
        elif m.group("em_us") is not None:
            out.append(emitter.em(recurse(m.group("em_us"))))
        elif m.group("em_star") is not None:
            out.append(emitter.em(recurse(m.group("em_star"))))
        elif m.group("xref") is not None:
            number = m.group("xref_num")
            if not chapters:
                # Not a book chapter -- a standalone document, or the README on
                # the site. There is no chapter namespace to resolve against, so
                # "Appendix A" is three words rather than a link into nowhere.
                out.append(emitter.text(m.group("xref")))
                pos = m.end()
                continue
            if number not in chapters:
                fail(
                    path,
                    lineno,
                    f"{m.group('xref')!r} names a chapter this book does not have "
                    f"(it has {', '.join(sorted(chapters))})",
                )
            out.append(emitter.xref(m.group("xref"), number))
        elif m.group("mark") is not None:
            out.append(emitter.mark(m.group("mark")))
        pos = m.end()

    tail = text[pos:]
    emitter.check_prose(tail, path, lineno)
    out.append(emitter.text(tail))
    return "".join(out)


# ---------------------------------------------------------------------------
# Block conversion
# ---------------------------------------------------------------------------

_FIGURE_RE = re.compile(r"^!\[(?P<caption>[^\]]*)\]\((?P<src>[^)]+)\)$")
_HEADING_RE = re.compile(r"^(?P<hashes>#{1,6})\s+(?P<text>.*)$")
_FENCE_RE = re.compile(r"^(?P<indent>\s*)```(?P<lang>[A-Za-z0-9_+-]*)\s*$")
_CALLOUT_RE = re.compile(r"^>\s*\[!(?P<kind>[A-Z]+)\]\s*$")
_ULI_RE = re.compile(r"^(?P<indent> *)[-*]\s+(?P<text>.*)$")
_OLI_RE = re.compile(r"^(?P<indent> *)\d+\.\s+(?P<text>.*)$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?[\s:|-]+\|[\s:|-]*$")
_DIRECTIVE_RE = re.compile(r"^<!--\s*(?P<name>.+?)\s*-->$")
# The directives a book may write, and what each does to the table below it.
# Both take the output's fixed field widths; `index` additionally turns the
# right-hand column's section links into locators.
_TABLE_DIRECTIVES = {"table: fields": "fields", "table: index": "index"}
# One index cell: `[A Section (4)](04-file.md#slug), [Another](#slug)`.
_LINK = r"\[[^\]]+\]\([^)]+\)"
_LINK_RE = re.compile(r"\[(?P<text>[^\]]+)\]\((?P<href>[^)]+)\)")
_LOCATOR_LIST_RE = re.compile(rf"^{_LINK}(?:,\s*{_LINK})*$")
# A cell boundary is an *unescaped* pipe. GFM spells a literal one `\|`, which
# several generated appendices need: a field of type `str | None` and config
# help that quotes its choices as `'cc'|'note'|'pc'` both carry one, and
# rewording them to dodge the delimiter would make the reference disagree with
# the program it documents.
_CELL_SPLIT_RE = re.compile(r"(?<!\\)\|")


@dataclass
class Chapter:
    path: Path
    number: str | None
    title: str
    # (ref, converted title) per `##`, so the opener page can link its bullets
    # at the sections they name.
    sections: list[tuple[SectionRef, str]] = field(default_factory=list)
    body: str = ""


class Converter:
    """Walks one chapter's Markdown body, emitting through an `Emitter`."""

    def __init__(
        self,
        path: Path,
        line_offset: int,
        emitter: Emitter,
        chapters: frozenset[str] = frozenset(),
        anchors: frozenset[str] = frozenset(),
    ) -> None:
        self.path = path
        self.line_offset = line_offset
        self.emitter = emitter
        self.chapters = chapters
        self.anchors = anchors
        self.sections: list[tuple[SectionRef, str]] = []
        self.title: str | None = None
        self.figures: list[str] = []
        # Repeated headings are disambiguated `-1`, `-2` the way GitHub does,
        # so the count has to run across the whole file rather than per call.
        self._slug_counts: dict[str, int] = {}

    def inline(self, text: str, index: int) -> str:
        """`convert_inline` with this chapter's path, line and book bound in."""
        return convert_inline(
            text, self.emitter, self.path, self.lineno(index), self.chapters, self.anchors
        )

    def lineno(self, index: int) -> int:
        return self.line_offset + index

    def convert(self, body: str) -> str:
        lines = body.split("\n")
        out: list[str] = []
        i = 0
        table_kind: str | None = None
        while i < len(lines):
            line = lines[i]
            if not line.strip():
                i += 1
                continue

            directive = _DIRECTIVE_RE.match(line.strip())
            if directive:
                # An HTML comment, so github.com renders nothing where it sits.
                # Both say the table below is a list of settings rather than a
                # grid of values, and should be set to the width every other
                # such table uses; see _TABLE_DIRECTIVES.
                name = directive.group("name")
                if name not in _TABLE_DIRECTIVES:
                    fail(self.path, self.lineno(i), f"unknown directive {name!r}")
                table_kind = _TABLE_DIRECTIVES[name]
                i += 1
                continue

            fence = _FENCE_RE.match(line)
            if fence:
                i = self._code_block(lines, i, fence, out)
                continue
            if _CALLOUT_RE.match(line.strip()) or line.startswith(">"):
                i = self._callout(lines, i, out)
                continue
            heading = _HEADING_RE.match(line)
            if heading:
                self._heading(heading, i, out)
                i += 1
                continue
            figure = _FIGURE_RE.match(line.strip())
            if figure:
                self._figure(figure, i, out)
                i += 1
                continue
            if "|" in line and i + 1 < len(lines) and _TABLE_SEP_RE.match(lines[i + 1]):
                i = self._table(lines, i, out, kind=table_kind)
                table_kind = None
                continue
            if table_kind:
                fail(
                    self.path, self.lineno(i), f"`table: {table_kind}` must be followed by a table"
                )
            if _ULI_RE.match(line) or _OLI_RE.match(line):
                i = self._list(lines, i, out)
                continue
            i = self._paragraph(lines, i, out)
        return "\n".join(out)

    # -- blocks -------------------------------------------------------------

    def _heading(self, m: re.Match[str], index: int, out: list[str]) -> None:
        level = len(m.group("hashes"))
        text = m.group("text").strip()
        lineno = self.lineno(index)
        if level == 1:
            if self.title is not None:
                fail(self.path, lineno, "a chapter may only have one `# ` title")
            self.title = text
            return  # the opener page is emitted by the caller, from front matter
        if self.title is None:
            fail(self.path, lineno, "the first heading in a chapter must be `# Title`")
        if level > 4:
            fail(self.path, lineno, f"heading level {level} is deeper than the design supports")
        inline = self.inline(text, index)
        # Level 3 too: a scene type, `### Companding — `dac_curve`` and
        # `### `sid_panning`` are all `###`, and that is the granularity a
        # reader looks things up at.
        ref = SectionRef(self.path.stem, self._slug(text)) if level in (2, 3) else None
        if level == 2 and ref is not None:
            # Converted, not raw: the opener page lists these, and a section
            # called `[hardware]` was reaching it with its backticks still on.
            self.sections.append((ref, inline))
        out.append(self.emitter.heading(level, inline, ref))

    def _slug(self, text: str) -> str:
        """This heading's anchor, disambiguated against the ones before it."""
        slug = heading_slug(text)
        count = self._slug_counts.get(slug, 0)
        self._slug_counts[slug] = count + 1
        return slug if count == 0 else f"{slug}-{count}"

    def _figure(self, m: re.Match[str], index: int, out: list[str]) -> None:
        src, caption = m.group("src"), m.group("caption")
        lineno = self.lineno(index)
        if not caption.strip():
            fail(self.path, lineno, "figures need a caption")
        target = (self.path.parent / src).resolve()
        if not target.exists():
            fail(self.path, lineno, f"figure not found: {src}")
        self.figures.append(src)
        out.append(self.emitter.figure(src, target, self.inline(caption, index)))

    def _code_block(self, lines: list[str], i: int, fence: re.Match[str], out: list[str]) -> int:
        lang = fence.group("lang")
        start = i
        i += 1
        content: list[str] = []
        while i < len(lines) and not lines[i].strip().startswith("```"):
            content.append(lines[i])
            i += 1
        if i >= len(lines):
            fail(self.path, self.lineno(start), "unterminated code fence")
        out.append(self.emitter.code_block("\n".join(content), lang))
        return i + 1

    def _callout(self, lines: list[str], i: int, out: list[str]) -> int:
        head = _CALLOUT_RE.match(lines[i].strip())
        if not head:
            fail(
                self.path,
                self.lineno(i),
                "block quotes are only used for callouts; start with `> [!NOTE]`",
            )
        kind = head.group("kind")
        if kind not in CALLOUT_KINDS:
            fail(self.path, self.lineno(i), f"unknown callout {kind!r}; use one of {CALLOUT_KINDS}")
        i += 1
        inner: list[str] = []
        while i < len(lines) and lines[i].startswith(">"):
            inner.append(re.sub(r"^>\s?", "", lines[i]))
            i += 1
        nested = Converter(self.path, self.lineno(i), self.emitter, self.chapters, self.anchors)
        nested.title = self.title  # inherit, so heading checks stay quiet
        body = nested.convert("\n".join(inner))
        self.figures.extend(nested.figures)
        out.append(self.emitter.callout(kind, body))
        return i

    def _locators(self, text: str, index: int) -> str | None:
        """An index cell's section links, spelled as locators.

        The Markdown points at sections because that is the only locator
        github.com has. In the PDF the reader wants a page, which the template
        resolves from the same label the link already names; on the web the
        section's own name is the locator, so each entry carries its converted
        link text alongside the ref and an emitter takes whichever it sets.
        One source serves every output, and none carries a locator the others
        cannot follow. Returns None for a cell that is not a plain list of
        links, which is every other table in the book.
        """
        if not _LOCATOR_LIST_RE.match(text.strip()):
            return None
        entries = []
        for m in _LINK_RE.finditer(text):
            ref = resolve_section_href(m.group("href"), self.path, self.lineno(index), self.anchors)
            if ref is None:
                return None
            entries.append((ref, self.inline(m.group("text"), index)))
        return self.emitter.locators(entries)

    def _table(self, lines: list[str], i: int, out: list[str], *, kind: str | None = None) -> int:
        start = i

        def cells(row: str) -> list[str]:
            # Unescape after splitting, and before any inline parsing, which is
            # the order GFM specifies -- so a pipe reaches a code span as a
            # pipe rather than as a backslash the span would print literally.
            return [
                c.strip().replace(r"\|", "|") for c in _CELL_SPLIT_RE.split(row.strip().strip("|"))
            ]

        header = cells(lines[i])
        aligns = []
        for spec in cells(lines[i + 1]):
            left, right = spec.startswith(":"), spec.endswith(":")
            aligns.append("center" if left and right else "right" if right else "left")
        i += 2
        rows: list[list[str]] = []
        while i < len(lines) and lines[i].strip().startswith("|"):
            rows.append(cells(lines[i]))
            i += 1

        ncols = len(header)
        for row in rows:
            if len(row) != ncols:
                fail(
                    self.path,
                    self.lineno(i),
                    f"table row has {len(row)} cells, header has {ncols}",
                )

        if kind and ncols != 2:
            fail(self.path, self.lineno(start), f"a {kind} table takes 2 columns, not {ncols}")

        def cell(text: str, index: int, column: int = 0) -> str:
            if kind == "index" and column == 1:
                locators = self._locators(text, index)
                if locators is not None:
                    return locators
            return self.inline(text, index)

        converted_header = [cell(c, start) for c in header]
        converted_rows = [
            [cell(c, offset, x) for x, c in enumerate(row)]
            for offset, row in enumerate(rows, start=start + 2)
        ]
        out.append(self.emitter.table(converted_header, converted_rows, aligns, kind))
        return i

    def _list(self, lines: list[str], i: int, out: list[str]) -> int:
        items: list[ListItem] = []
        while i < len(lines):
            line = lines[i]
            uli, oli = _ULI_RE.match(line), _OLI_RE.match(line)
            if uli or oli:
                m = uli or oli
                assert m is not None
                items.append(
                    ListItem(
                        indent=m.group("indent"),
                        ordered=oli is not None and uli is None,
                        text=self.inline(m.group("text"), i),
                    )
                )
                i += 1
            elif line.strip() and line.startswith((" ", "\t")):
                items.append(
                    ListItem(
                        indent="",
                        ordered=False,
                        text=self.inline(line.strip(), i),
                        continuation=True,
                    )
                )
                i += 1
            else:
                break
        out.append(self.emitter.list_block(items))
        return i

    def _paragraph(self, lines: list[str], i: int, out: list[str]) -> int:
        if self.title is None:
            fail(self.path, self.lineno(i), "the first line of a chapter must be `# Title`")
        buf: list[str] = []
        start = i
        while i < len(lines) and lines[i].strip():
            line = lines[i]
            if _HEADING_RE.match(line) or _FENCE_RE.match(line) or line.startswith(">"):
                break
            if _ULI_RE.match(line) or _OLI_RE.match(line):
                break
            if _FIGURE_RE.match(line.strip()):
                break
            buf.append(line.strip())
            i += 1
        out.append(self.emitter.paragraph(self.inline(" ".join(buf), start)))
        return i


# ---------------------------------------------------------------------------
# Book discovery
# ---------------------------------------------------------------------------


def chapter_numbers(paths: list[Path]) -> frozenset[str]:
    """Every chapter number in a book, read from front matter alone.

    A first pass over the whole book, because a cross-reference in chapter 1 can
    name an appendix that has not been loaded yet -- and a reference to a
    chapter that does not exist has to fail the build rather than reach the
    output as a link into nowhere.
    """
    numbers = set()
    for path in paths:
        fields, _, _ = parse_front_matter(path.read_text(encoding="utf-8"), path)
        number = fields.get("number")
        if number is not None:
            numbers.add(number)
    return frozenset(numbers)


def section_anchors(paths: list[Path]) -> frozenset[str]:
    """Every section label a book defines, read from its Markdown alone.

    A second whole-book pass, for the same reason `chapter_numbers` is one: a
    link in chapter 1 can name a section in an appendix that has not been
    converted yet. Sixteen files of stdlib regex, so the cost is nothing.
    """
    labels: set[str] = set()
    for path in paths:
        _, body, _ = parse_front_matter(path.read_text(encoding="utf-8"), path)
        labels.update(section_label(path.stem, slug) for slug in file_section_slugs(body))
    return frozenset(labels)


def load_chapter(
    path: Path,
    emitter: Emitter,
    chapters: frozenset[str] = frozenset(),
    anchors: frozenset[str] = frozenset(),
) -> Chapter:
    text = path.read_text(encoding="utf-8")
    fields, body, offset = parse_front_matter(text, path)
    number = fields.get("number")

    conv = Converter(path, offset, emitter, chapters, anchors)
    converted = conv.convert(body)
    if conv.title is None:
        fail(path, offset, "chapter has no `# Title`")
    if number is not None and not conv.sections:
        fail(path, offset, "a numbered chapter needs at least one `## Section`")
    return Chapter(
        path=path, number=number, title=conv.title, sections=conv.sections, body=converted
    )


def discover_chapters(book_dir: Path) -> list[Path]:
    """Chapter files, ordered by their numeric filename prefix."""
    paths = sorted(p for p in book_dir.glob("*.md") if re.match(r"^\d+-", p.name))
    if not paths:
        raise BookError(f"no NN-name.md chapter files found in {book_dir}")
    return paths


def load_book_toml(book_dir: Path) -> dict[str, str]:
    """The book's `[book]` table, checked against the layout it asks for.

    Everything the template needs must be present here: a book that lost its
    cover logo should fail the build rather than render a PDF with a blank
    front, which nobody notices until it is published.
    """
    path = book_dir / "book.toml"
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise BookError(f"cannot read {path}: {exc}") from exc

    book = data.get("book")
    if not isinstance(book, dict):
        raise BookError(f"{path} has no [book] table")
    layout = book.get("layout")
    if layout not in LAYOUT_KEYS:
        raise BookError(f"{path}: layout must be one of {sorted(LAYOUT_KEYS)}, not {layout!r}")
    missing = [key for key in ("output", *LAYOUT_KEYS[layout]) if not book.get(key)]
    if missing:
        raise BookError(f"{path}: [book] is missing {', '.join(missing)}")
    return book


def book_version() -> str:
    """The c64cast version this book documents, read from `pyproject.toml`.

    Deliberately NOT `importlib.metadata.version("c64cast")`: that answers
    "what is installed in the interpreter running this script", which during a
    release build is whatever `uv sync` last resolved and may lag the version
    being cut. `pyproject.toml` is the single source of truth the release tag
    is checked against, so the number on the cover is the number on the tin.

    A book only ever builds from a checkout (it reads docs/<book>/*.md from
    REPO_ROOT), so a missing pyproject is a broken tree, not a supported case.
    """
    try:
        data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    except OSError as exc:
        raise BookError(f"cannot read {PYPROJECT}: {exc}") from exc
    version = data.get("project", {}).get("version")
    if not isinstance(version, str) or not version:
        raise BookError(f"{PYPROJECT} has no [project] version")
    return version
