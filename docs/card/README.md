# The c64cast Performance Card

One sheet, printed double-sided, that lives next to the controller. It is not a
third book: everything on it is already in the [Programmer's Reference
Guide](../reference/README.md), compressed to what a performer needs at a
glance in a dark room — the keyboard, the gestures, the default MIDI map, the
pad chords and lights, every live target, the clip grid, the console routes, and
the four commands worth running before the doors open.

## Reading it

The Markdown renders on github.com as-is; start at
[`01-controls.md`](01-controls.md), or read it online at
[kfox.github.io/c64cast/card/](https://kfox.github.io/c64cast/card/). For the
printable version, download
[the PDF](https://github.com/kfox/c64cast/releases/latest/download/c64cast-performance-card.pdf)
(always the newest release), or render it from a checkout:

```bash
make card           # -> docs/card/c64cast-performance-card.pdf
make books          # every book at once
```

## What is different about it

It takes the `card` layout rather than the `guide` one, which is the same
palette, faces and table styling with none of the apparatus of a bound book: no
cover, no colophon, no contents, no chapter openers. It opens on its first line
and is set two-up on US Letter at 8.5pt, with a footer carrying the version —
a card pinned to a desk outlives the release it was printed for.

| File | Is |
|---|---|
| `01-controls.md` | Every control surface, and what it is mapped to |
| `02-live-targets.md` | **Generated** — every parameter a `param` mapping can name |
| `03-performing.md` | Clips, tempo, looks, the console, WLED, ensembles |
| `book.toml` | Layout, artifact name, and the footer's title |

`02-live-targets.md` is written by
[`scripts/gen_reference_appendices.py`](../../scripts/gen_reference_appendices.py)
in the same pass as the reference guide's appendices, from the same registry
that answers `--describe`. Regenerate it with `make reference-appendices`;
`tests/test_reference_appendices.py` fails if the committed file has drifted.

**Two pages is the specification, not an observation.** A third page is a
sheet a venue has to keep track of. Adding a section means cutting one, and
`make card` is the check.

## How the build works

Same pipeline as every book: see [`docs/shared/`](../shared/README.md) for the
converter, the template and the two layouts.
