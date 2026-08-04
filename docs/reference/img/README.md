# Reference guide diagrams

Every image here is drawn by
[`scripts/make_reference_diagrams.py`](../../../scripts/make_reference_diagrams.py)
and committed, because the release renders the books with
`uv run --no-project` and cannot regenerate anything that imports
`c64cast`. Redraw them with `make reference-figures` after changing a
diagram, and commit the result.

These are drawings rather than captures — the guide's `img/` is the
other kind. They are set in the books' own two faces from
`docs/shared/fonts/` and use the template's palette, except inside the
cell diagram, whose subject is which C64 colour each attribute byte
holds.

| Figure | Chapter and section | Shows |
|---|---|---|
| `fig-1-1-ladder.png` | 1 · The Precedence Ladder | The five layers, and the rung an ensemble inserts |
| `fig-3-1-pipeline.png` | 3 · From Frame to Screen | The twelve steps, and where each setting enters |
| `fig-3-2-cells.png` | 3 · The Six Display Modes | One hardware cell per mode, with the bytes that colour it |
| `fig-4-1-audio.png` | 4 · Two Ways Out | The DAC path against the sampler path, and what each costs |
| `fig-5-1-memory.png` | 5 · What Lands in Memory | The 64 KB during a bitmap scene, banks stacked |

The memory map is schematic in one respect: a region the size of an
interrupt handler is a fraction of a pixel wide at 16 KB to the plate,
so every region is drawn at a minimum width.
