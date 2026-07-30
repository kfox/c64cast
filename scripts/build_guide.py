#!/usr/bin/env python3
"""Render docs/guide/*.md into a single Typst source for the User's Guide PDF.

The Markdown in docs/guide/ is the guide's only source. It is ordinary
GitHub-flavoured Markdown -- it renders correctly on github.com as-is, and
nothing about the *look* of the PDF is decided here. This module only
translates constructs; docs/guide/template.typ owns the design.

    python scripts/build_guide.py                 # write the .typ
    python scripts/build_guide.py --check         # parse only, write nothing

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
    - / 1.                     lists, nestable by indentation
    **bold** *italic* `code` [text](url)

Per-file YAML front matter carries only what Markdown cannot express:

    ---
    number: 1          # omit entirely for front matter (Quick Start, etc.)
    ---                # "A"/"B" for appendices

The chapter's title comes from its `# H1` and the opener page's section list
is derived from its `##` headings, so neither can drift from the prose.
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import NoReturn

REPO_ROOT = Path(__file__).resolve().parent.parent
GUIDE_DIR = REPO_ROOT / "docs" / "guide"
BOOK_TOML = GUIDE_DIR / "book.toml"
PYPROJECT = REPO_ROOT / "pyproject.toml"
DEFAULT_OUT = GUIDE_DIR / "c64cast-users-guide.typ"

CALLOUT_KINDS = ("NOTE", "TIP", "WARNING", "IMPORTANT", "CAUTION")

# Characters that carry meaning in Typst markup and so must be escaped in any
# run of literal prose. `-` and `.` are deliberately absent: Typst turns `--`
# into an en dash and `...` into an ellipsis, which is what we want in prose.
# Command-line flags always live in backticks, where no substitution happens;
# `check_prose` below enforces that.
_TYPST_SPECIAL = set("\\#$*_`<>@[]~")


class GuideError(Exception):
    """A problem in the guide source. Always fatal -- never render partial prose."""


def fail(path: Path, lineno: int, message: str) -> NoReturn:
    raise GuideError(f"{path.relative_to(REPO_ROOT)}:{lineno}: {message}")


# ---------------------------------------------------------------------------
# Front matter
# ---------------------------------------------------------------------------


def parse_front_matter(text: str, path: Path) -> tuple[dict[str, str], str, int]:
    """Split leading `---` YAML front matter from the body.

    Only `key: value` scalars are supported -- that is the whole vocabulary the
    guide needs, and a full YAML parser would be a dependency for nothing.
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

    allowed = {"number"}
    for key in fields:
        if key not in allowed:
            fail(path, 1, f"unknown front matter key {key!r} (allowed: {sorted(allowed)})")
    return fields, "\n".join(lines[end + 1 :]), end + 2


# ---------------------------------------------------------------------------
# Inline conversion
# ---------------------------------------------------------------------------


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


# Ordered: the first pattern to match at a position wins. Code spans come first
# so that markup inside them is never interpreted.
_INLINE = re.compile(
    r"""
      \\(?P<esc>[\\`*_\[\]()#+\-.!<>~{}])
    | (?P<code>`{1,3})(?P<code_body>.+?)(?P=code)
    | <kbd>(?P<kbd>.+?)</kbd>
    | !\[(?P<img_alt>[^\]]*)\]\((?P<img_src>[^)]+)\)
    | \[(?P<link_text>[^\]]+)\]\((?P<link_href>[^)]+)\)
    | \*\*(?P<bold>.+?)\*\*
    | (?<![A-Za-z0-9])_(?P<em_us>[^_]+)_(?![A-Za-z0-9])
    | \*(?P<em_star>[^*]+)\*
    """,
    re.VERBOSE | re.DOTALL,
)


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


def convert_inline(text: str, path: Path, lineno: int) -> str:
    out: list[str] = []
    pos = 0
    for m in _INLINE.finditer(text):
        literal = text[pos : m.start()]
        check_prose(literal, path, lineno)
        out.append(escape(literal))

        if m.group("esc") is not None:
            # A CommonMark backslash escape. This alternative is FIRST in the
            # pattern so that `Jost\*` yields a literal asterisk instead of
            # leaving a stray backslash and opening an emphasis run that eats
            # prose until the next `*` several sentences later.
            out.append(escape(m.group("esc")))
        elif m.group("code") is not None:
            out.append(typst_inline_raw(m.group("code_body")))
        elif m.group("kbd") is not None:
            out.append(f"#kbd[{escape(m.group('kbd'))}]")
        elif m.group("img_src") is not None:
            fail(path, lineno, "images must stand alone in their own paragraph")
        elif m.group("link_text") is not None:
            href = m.group("link_href")
            label = convert_inline(m.group("link_text"), path, lineno)
            out.append(f"#link({typst_string(href)})[{label}]")
        elif m.group("bold") is not None:
            out.append(f"*{convert_inline(m.group('bold'), path, lineno)}*")
        elif m.group("em_us") is not None:
            out.append(f"_{convert_inline(m.group('em_us'), path, lineno)}_")
        elif m.group("em_star") is not None:
            out.append(f"_{convert_inline(m.group('em_star'), path, lineno)}_")
        pos = m.end()

    tail = text[pos:]
    check_prose(tail, path, lineno)
    out.append(escape(tail))
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


@dataclass
class Chapter:
    path: Path
    number: str | None
    title: str
    sections: list[str] = field(default_factory=list)
    body: str = ""


class Converter:
    """Converts one chapter's Markdown body into Typst markup."""

    def __init__(self, path: Path, line_offset: int) -> None:
        self.path = path
        self.line_offset = line_offset
        self.sections: list[str] = []
        self.title: str | None = None
        self.figures: list[str] = []

    def lineno(self, index: int) -> int:
        return self.line_offset + index

    def convert(self, body: str) -> str:
        lines = body.split("\n")
        out: list[str] = []
        i = 0
        while i < len(lines):
            line = lines[i]
            if not line.strip():
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
                i = self._table(lines, i, out)
                continue
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
        if level == 2:
            self.sections.append(text)
        if level > 4:
            fail(self.path, lineno, f"heading level {level} is deeper than the design supports")
        inline = convert_inline(text, self.path, lineno)
        out.append(f"#heading(level: {level})[{inline}]\n")

    def _figure(self, m: re.Match[str], index: int, out: list[str]) -> None:
        src, caption = m.group("src"), m.group("caption")
        lineno = self.lineno(index)
        if not caption.strip():
            fail(self.path, lineno, "figures need a caption")
        target = (self.path.parent / src).resolve()
        if not target.exists():
            fail(self.path, lineno, f"figure not found: {src}")
        self.figures.append(src)
        body = convert_inline(caption, self.path, lineno)
        out.append(f"#screenshot({typst_string(src)}, [{body}])\n")

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
        text = "\n".join(content)
        lang_arg = f", lang: {typst_string(lang)}" if lang else ""
        out.append(f"#raw({typst_string(text)}, block: true{lang_arg})\n")
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
        nested = Converter(self.path, self.lineno(i))
        nested.title = self.title  # inherit, so heading checks stay quiet
        body = nested.convert("\n".join(inner))
        self.figures.extend(nested.figures)
        out.append(f'#callout(kind: "{kind}")[\n{body}\n]\n')
        return i

    def _table(self, lines: list[str], i: int, out: list[str]) -> int:
        def cells(row: str) -> list[str]:
            return [c.strip() for c in row.strip().strip("|").split("|")]

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

        def cell(text: str, lineno: int) -> str:
            return f"[{convert_inline(text, self.path, lineno)}]"

        parts = [f"#table(\n  columns: {ncols},", f"  align: ({', '.join(aligns)},),"]
        # A real `table.header`, not just a first row: it repeats when a table
        # splits across a page, and it stops Typst from stranding the header at
        # the foot of one page with its body at the top of the next.
        parts.append("  table.header(" + ", ".join(cell(c, self.lineno(i)) for c in header) + "),")
        for row in rows:
            parts.append("  " + ", ".join(cell(c, self.lineno(i)) for c in row) + ",")
        parts.append(")\n")
        out.append("\n".join(parts))
        return i

    def _list(self, lines: list[str], i: int, out: list[str]) -> int:
        """Emit a list, preserving indentation so Typst reproduces the nesting."""
        block: list[str] = []
        while i < len(lines):
            line = lines[i]
            uli, oli = _ULI_RE.match(line), _OLI_RE.match(line)
            if uli or oli:
                m = uli or oli
                assert m is not None
                marker = "-" if uli else "+"
                indent = m.group("indent")
                text = convert_inline(m.group("text"), self.path, self.lineno(i))
                block.append(f"{indent}{marker} {text}")
                i += 1
            elif line.strip() and line.startswith((" ", "\t")):
                # A continuation line of the previous item.
                block.append("  " + convert_inline(line.strip(), self.path, self.lineno(i)))
                i += 1
            else:
                break
        out.append("\n".join(block) + "\n")
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
        text = " ".join(buf)
        out.append(convert_inline(text, self.path, self.lineno(start)) + "\n")
        return i


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------


def load_chapter(path: Path) -> Chapter:
    text = path.read_text(encoding="utf-8")
    fields, body, offset = parse_front_matter(text, path)
    number = fields.get("number")

    conv = Converter(path, offset)
    typst = conv.convert(body)
    if conv.title is None:
        fail(path, offset, "chapter has no `# Title`")
    if number is not None and not conv.sections:
        fail(path, offset, "a numbered chapter needs at least one `## Section`")
    return Chapter(path=path, number=number, title=conv.title, sections=conv.sections, body=typst)


def discover_chapters(guide_dir: Path) -> list[Path]:
    """Chapter files, ordered by their numeric filename prefix."""
    paths = sorted(p for p in guide_dir.glob("*.md") if re.match(r"^\d+-", p.name))
    if not paths:
        raise GuideError(f"no NN-name.md chapter files found in {guide_dir}")
    return paths


def typst_list(items: list[str]) -> str:
    return "(" + ", ".join(typst_string(i) for i in items) + ("," if items else "") + ")"


def guide_version() -> str:
    """The c64cast version this guide documents, read from `pyproject.toml`.

    Deliberately NOT `importlib.metadata.version("c64cast")`: that answers
    "what is installed in the interpreter running this script", which during a
    release build is whatever `uv sync` last resolved and may lag the version
    being cut. `pyproject.toml` is the single source of truth the release tag
    is checked against, so the number on the cover is the number on the tin.

    The guide only ever builds from a checkout (it reads docs/guide/*.md from
    REPO_ROOT), so a missing pyproject is a broken tree, not a supported case.
    """
    try:
        data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    except OSError as exc:
        raise GuideError(f"cannot read {PYPROJECT}: {exc}") from exc
    version = data.get("project", {}).get("version")
    if not isinstance(version, str) or not version:
        raise GuideError(f"{PYPROJECT} has no [project] version")
    return version


def build(guide_dir: Path = GUIDE_DIR) -> str:
    book = tomllib.loads(BOOK_TOML.read_text(encoding="utf-8"))["book"]

    chapters = [load_chapter(p) for p in discover_chapters(guide_dir)]
    numbered = [c for c in chapters if c.number is not None]
    if not numbered:
        raise GuideError("the guide has no numbered chapters")

    colophon = (guide_dir / "colophon.md").read_text(encoding="utf-8")
    colophon_conv = Converter(guide_dir / "colophon.md", 1)
    colophon_conv.title = ""  # the colophon is bare prose, no heading
    colophon_typst = colophon_conv.convert(colophon)

    out: list[str] = [
        "// GENERATED by scripts/build_guide.py -- do not edit.",
        "// Source: docs/guide/*.md   Design: docs/guide/template.typ",
        "",
        '#import "template.typ": *',
        "",
        "#show: guide.with(",
        f"  title: {typst_string(book['title'])},",
        f"  subtitle: {typst_string(book['subtitle'])},",
        f"  tagline: {typst_string(book['tagline'])},",
        f"  logo: {typst_string(book['logo'])},",
        # Not in book.toml: the version is not book-level metadata somebody
        # edits, it is whatever release this build documents.
        f"  version: {typst_string(guide_version())},",
        ")",
        "",
        f"#colophon[\n{colophon_typst}\n]",
        "",
        "#frontmatter()",
        "",
        "#toc()",
        "",
    ]

    seen_numbered = False
    for chapter in chapters:
        if chapter.number is not None and not seen_numbered:
            out += ["#mainmatter()", ""]
            seen_numbered = True
        number_arg = "none" if chapter.number is None else typst_string(chapter.number)
        out.append(
            f"#chapter(\n"
            f"  number: {number_arg},\n"
            f"  title: {typst_string(chapter.title)},\n"
            f"  contents: {typst_list(chapter.sections)},\n"
            f")"
        )
        out += ["", chapter.body, ""]

    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help="output .typ path")
    ap.add_argument("--check", action="store_true", help="parse only; write nothing")
    args = ap.parse_args()

    try:
        typst = build()
    except GuideError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.check:
        print("guide source OK")
        return 0

    args.out.write_text(typst, encoding="utf-8")
    print(f"wrote {args.out.relative_to(REPO_ROOT)} ({len(typst.splitlines())} lines)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
