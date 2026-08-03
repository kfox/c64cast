# The c64cast Programmer's Reference Guide

The second volume. Where the [User's Guide](../guide/README.md) is a book you
read in order, this is the one you open at the page you need: the rules of the
configuration language, the vocabulary of scenes and overlays, the display and
sound paths in full, what lands in the Commodore's memory, and exhaustive
tables of every field, key, parameter and flag.

Its structure is an homage to the *Commodore 64 Programmer's Reference Guide* —
in particular that book's willingness to organise by subsystem rather than by
audience, and to print the whole table rather than a useful subset.

> [!NOTE]
> Chapters 4 to 6 are outlines at the moment; they land with the prose change
> above this one in the stack. Chapters 1 to 3 and the appendices are
> complete.

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

## The generated appendices

Appendices A to H are **not written by hand**. They are read out of the same
definitions that answer `--describe`, `--compat` and `--print-schema`, by
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

They carry `generated: true` in their front matter and are committed, so the
release can render the PDFs without resolving the project environment. After
changing a config field, an overlay, a generator, an effect, a CLI flag or an
example config:

```bash
make reference-appendices
```

`tests/test_reference_appendices.py` fails if the committed files drift from a
fresh run, so a forgotten regeneration is caught in CI rather than in print.
That test also converts each one, which is what catches help text that a
terminal is happy with and a typesetter is not — an unbackticked `--flag`
becomes an en dash, and a bare `|` splits a table row.

## How the build works

Same pipeline as every book: see [`docs/shared/`](../shared/README.md) for the
converter, the template and the two layouts.
