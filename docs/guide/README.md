# The c64cast User's Guide

A friendly, start-from-nothing introduction to c64cast, written to be read in
order. Where the [Programmer's Reference Guide](../reference/README.md) is a
book you consult, this is one you read.

Its structure and typography are an affectionate homage to the *Commodore 64
Ultimate User's Guide* — in particular that book's method of introducing
complexity incrementally: a Quick Start that gets a picture on screen in five
minutes, a page of one-line things worth trying, a warm introduction, and
only then numbered chapters that each assume nothing beyond the ones before.

## Reading it

The Markdown is the guide. Start at
[`01-quick-start.md`](01-quick-start.md) and work forward; the files are
numbered in reading order. Everything renders on github.com as-is.

For the typeset version, download the PDF from
[the latest release](https://github.com/kfox/c64cast/releases/latest) — it is
stamped on the cover with the version it documents, so a saved copy can always
be matched to an install.

To render it from a checkout instead:

```bash
make guide          # -> docs/guide/c64cast-users-guide.pdf
```

## How the build works

```
docs/guide/*.md          the guide (the only source)
      |
      +--> scripts/build_book.py  --> c64cast-users-guide.typ
                                          |
      docs/shared/template.typ  --------->+--> typst --> .pdf
```

`build_book.py` translates constructs; the [shared
template](../shared/README.md) owns every design decision. Neither the prose
nor the converter decides what anything looks like, so the whole book can be
restyled by editing one file — and so can every other book, because they share
it.

The generated `.typ` and the `.pdf` are build artifacts and are gitignored.
The Markdown and the figures are tracked.

| File | Is |
|---|---|
| `NN-*.md` | The chapters, in reading order |
| `colophon.md` | The copyright and credits page |
| `book.toml` | Layout, artifact name, and cover metadata (title, subtitle, tagline, logo) |
| `img/` | Figures, plus their [shot list](img/README.md) |

## Writing for it

The supported Markdown is deliberately small, and anything outside it is a
**hard error** rather than a silent drop — a manual that quietly loses a
paragraph is worse than one that fails to build. Check your source without
rendering:

```bash
uv run python scripts/build_book.py --book-dir docs/guide --check
```

| Write | Get |
|---|---|
| `# Title` | The chapter title. Exactly one, first line of the body |
| `## Section` | A blue section heading, and an entry on the opener page |
| `### Subsection` | A bold subsection |
| `> [!NOTE]`, `> [!TIP]`, `> [!WARNING]` | A ruled callout box |
| `<kbd>RETURN</kbd>` | A keycap chip |
| `![Caption.](img/fig-2-1-x.png)` | A framed figure. Must be alone in its paragraph |
| ` ```toml ` | A code block |
| `| a | b |` | A table |
| `<!-- table: fields -->` | The table below it is a settings list — name, type and default stacked in a fixed column, description in the rest. Invisible on github.com |
| `a<br>b` | A line break inside a table cell |
| `- ` / `1. ` | Lists, nestable by indentation |
| `**bold**`, `*italic*`, `` `code` ``, `[text](url)` | As expected |
| `Chapter 4`, `Appendix F` | A link to that chapter's opener page |
| `✓`, `→` | Drawn marks — the body face carries neither |

Four rules that are not obvious:

- **Put command-line flags in backticks.** Typst turns a bare `--` in prose
  into an en dash, so `--config` outside a code span would render wrong. The
  converter rejects it rather than mangling it.
- **A chapter's title and its opener-page contents are derived**, from the
  `# H1` and the `##` headings respectively. Neither can drift from the
  prose because neither is written twice.
- **A cross-reference is checked.** "Appendix F" becomes a link to that
  opener page, and naming a chapter the book does not have fails the build —
  which is what catches a renumbering the prose was not told about.
- **A character outside the two vendored faces will not be drawn at all.**
  Typst's own fallback is off, so there is no substitute and no warning;
  `tests/test_book_fonts.py` fails instead. Two marks the books lean on, `✓`
  and `→`, are drawn by the template rather than set.

Front matter carries only the chapter number, and only numbered chapters
need it:

```markdown
---
number: 2
---
```

Front-matter chapters (Quick Start, Fun Things to Try!, Introduction) omit
it entirely and are rendered as plain headings rather than full-page
openers, which is what the original does.

## Figures

Every figure is currently a generated placeholder that names, in the image
itself, the capture it is standing in for. Layout is therefore already
final, and finishing a figure is a file replacement:

```bash
make guide-figures      # redraw placeholders (real captures are left alone)
```

See [`img/README.md`](img/README.md) for the shot list.

## Fonts

Both faces are [Open Font License](https://openfontlicense.org/) and live in
[`../shared/fonts/`](../shared/fonts/README.md), committed alongside their
licences:

| Face | Used for | Licence |
|---|---|---|
| [Jost*](https://github.com/indestructible-type/Jost) | body, headings | OFL 1.1 (`OFL-Jost.txt`) |
| [Inconsolata](https://github.com/googlefonts/Inconsolata) | code, keycaps | OFL 1.1 (`OFL-Inconsolata.txt`) |

`make guide` always passes `--font-path docs/shared/fonts` to Typst, so the PDF
renders identically from a fresh checkout on any platform — nothing depends on
what happens to be installed. The template names these two families and turns
Typst's own fallback off, so a build that loses the font path fails rather than
quietly substituting a different face, and a character in neither is caught by
`tests/test_book_fonts.py` rather than by a reader.

The original book is set in MegaGlacial, which is commercial and is
deliberately neither used nor shipped here. Jost\* is a free geometric sans in
the same Futura lineage — single-story `a`, circular bowls, near-uniform
stroke. The mono face needs no substitute: Inconsolata is what the original
uses, and it is OFL already.
