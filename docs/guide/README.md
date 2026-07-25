# The c64cast User's Guide

A friendly, start-from-nothing introduction to c64cast, written to be read in
order. Where [`usage.md`](../usage.md) is a reference you consult,
this is a guide you read.

Its structure and typography are an affectionate homage to the *Commodore 64
Ultimate User's Guide* — in particular that book's method of introducing
complexity incrementally: a Quick Start that gets a picture on screen in five
minutes, a page of one-line things worth trying, a warm introduction, and
only then numbered chapters that each assume nothing beyond the ones before.

## Reading it

The Markdown is the guide. Start at
[`01-quick-start.md`](01-quick-start.md) and work forward; the files are
numbered in reading order. Everything renders on github.com as-is.

For the typeset version, build the PDF:

```bash
make guide          # -> docs/guide/c64cast-users-guide.pdf
```

## How the build works

```
docs/guide/*.md          the guide (the only source)
      |
      +--> scripts/build_guide.py  --> c64cast-users-guide.typ
                                          |
      docs/guide/template.typ  ----------->+--> typst --> .pdf
```

`build_guide.py` translates constructs; `template.typ` owns every design
decision. Neither the prose nor the converter decides what anything looks
like, so the whole book can be restyled by editing one file.

The generated `.typ` and the `.pdf` are build artifacts and are gitignored.
The Markdown, the template and the figures are tracked.

| File | Is |
|---|---|
| `NN-*.md` | The chapters, in reading order |
| `colophon.md` | The copyright and credits page |
| `book.toml` | Cover metadata (title, subtitle, tagline, logo) |
| `template.typ` | The entire visual language |
| `img/` | Figures, plus their [shot list](img/README.md) |

## Writing for it

The supported Markdown is deliberately small, and anything outside it is a
**hard error** rather than a silent drop — a manual that quietly loses a
paragraph is worse than one that fails to build. Check your source without
rendering:

```bash
uv run python scripts/build_guide.py --check
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
| `- ` / `1. ` | Lists, nestable by indentation |
| `**bold**`, `*italic*`, `` `code` ``, `[text](url)` | As expected |

Two rules that are not obvious:

- **Put command-line flags in backticks.** Typst turns a bare `--` in prose
  into an en dash, so `--config` outside a code span would render wrong. The
  converter rejects it rather than mangling it.
- **A chapter's title and its opener-page contents are derived**, from the
  `# H1` and the `##` headings respectively. Neither can drift from the
  prose because neither is written twice.

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
`fonts/`, committed alongside their licences:

| Face | Used for | Licence |
|---|---|---|
| [Jost*](https://github.com/indestructible-type/Jost) | body, headings | OFL 1.1 (`fonts/OFL-Jost.txt`) |
| [Inconsolata](https://github.com/googlefonts/Inconsolata) | code, keycaps | OFL 1.1 (`fonts/OFL-Inconsolata.txt`) |

`make guide` always passes `--font-path docs/guide/fonts` to Typst, so the PDF
renders identically from a fresh checkout on any platform — nothing depends on
what happens to be installed. `template.typ` names each family with no
fallback chain, so a build that loses the font path warns rather than quietly
substituting a different face.

The original book is set in MegaGlacial, which is commercial and is
deliberately neither used nor shipped here. Jost\* is a free geometric sans in
the same Futura lineage — single-story `a`, circular bowls, near-uniform
stroke. The mono face needs no substitute: Inconsolata is what the original
uses, and it is OFL already.
