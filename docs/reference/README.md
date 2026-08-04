# The c64cast Programmer's Reference Guide

The second volume. Where the [User's Guide](../guide/README.md) is a book you
read in order, this is the one you open at the page you need: the rules of the
configuration language, the vocabulary of scenes and overlays, the display and
sound paths in full, what lands in the Commodore's memory, and exhaustive
tables of every field, key, parameter and flag. A closing chapter covers
extending the program rather than configuring it.

Its structure is an homage to the *Commodore 64 Programmer's Reference Guide* —
in particular that book's willingness to organize by subsystem rather than by
audience, and to print the whole table rather than a useful subset.

## Reading it

The Markdown is the book. Start at
[`01-introduction.md`](01-introduction.md); the files are numbered in reading
order and everything renders on github.com as-is.

For the typeset version, download the PDF from
[the latest release](https://github.com/kfox/c64cast/releases/latest), or
render it from a checkout:

```bash
make reference      # -> docs/reference/c64cast-reference-guide.pdf
make books          # every book at once
```

## The generated appendices and index

Appendices A to I and the index are **not written by hand**. They are read out
of the same definitions that answer `--describe`, `--compat` and
`--print-schema`, by
[`scripts/gen_reference_appendices.py`](../../scripts/gen_reference_appendices.py):

| Appendix | Comes from |
|---|---|
| A — Configuration sections | `introspect.config_sections()` |
| B — Scene types | `introspect.scene_types()` |
| C — Overlays | `introspect.overlay_docs()` |
| D — Compatibility matrix | `introspect.compat_matrix()` |
| E — Generators and effects | the `generators` and `effects` registries |
| F — Live-tune targets | `introspect.live_targets()` |
| G — Command-line flags | the argparse parser in `cli.py` |
| H — Example configurations | the packaged `examples/`, read through `paths` |
| I — Optional extras | `doctor._EXTRAS` joined with `pyproject.toml` |
| The index | all of the above, crossed with the book's own Markdown |

The index is the only one that reads the book rather than the code. Every name
the program can utter goes in; the locators come from scanning the committed
chapters for that name in a code span or a heading and taking the section it
sits in, which is why a section renamed without a regeneration shows up as a
broken link rather than as a wrong page number. It carries no `number`, so it
renders after Appendix J as a plain heading rather than as Appendix K.

The same pass writes the [Performance Card](../card/README.md)'s live-target
table, which is the card's most drift-prone page.

They carry `generated: true` in their front matter and are committed, so the
release can render the PDFs without resolving the project environment. After
changing a config field, an overlay, a generator, an effect, a CLI flag, an
example config or an install extra:

```bash
make reference-appendices
```

`tests/test_reference_appendices.py` fails if the committed files drift from a
fresh run, so a forgotten regeneration is caught in CI rather than in print.
That test also converts each one, which is what catches help text that a
terminal is happy with and a typesetter is not — an unbackticked `--flag`
becomes an en dash, and a bare `|` splits a table row. `tests/test_book_fonts.py`
catches the third kind: a character the vendored faces have no glyph for, which
Typst leaves as a gap rather than reporting. A `⇒` in a generator's docstring
reached Appendix E that way.

Most of what these appendices contain is one shape — a thing with a name, a
type and a default, and a paragraph about it — so they are set as two columns
rather than four: the three identifying facts stacked in a fixed-width column,
and the description in all the measure that is left. `identity()` and
`fields_table()` in the generator build them, and the width itself is in
[`template.typ`](../shared/template.typ), which is where every other
measurement in the books lives.

## The diagrams

Five figures, in [`img/`](img/README.md), drawn by
[`scripts/make_reference_diagrams.py`](../../scripts/make_reference_diagrams.py):

```bash
make reference-figures
```

They are drawings rather than screen captures — the User's Guide's `img/` is
the other kind — and they are deliberately not Typst figures, because a Typst
drawing is invisible on github.com and the Markdown is the book. Pillow draws
them in the two vendored faces and the template's palette, so a diagram sits in
the same type and the same blue as the page around it, and the output is
committed for the same reason the appendices are: the release renders the PDFs
without the project environment.

`tests/test_reference_diagrams.py` fails if the script's copy of the palette
drifts from [`template.typ`](../shared/template.typ), if a committed PNG is no
longer the size the script draws, or if a figure is never referenced by a
chapter.

## How the build works

Same pipeline as every book: see [`docs/shared/`](../shared/README.md) for the
converter, the template and the two layouts.
