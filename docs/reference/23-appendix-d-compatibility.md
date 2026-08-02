---
number: D
generated: true
---

# Overlay and Display-Mode Compatibility

*Generated from the code by `scripts/gen_reference_appendices.py`.
Edits here are overwritten; run `make reference-appendices`.*

Which overlays attach to which display modes. A ✓ works; a · is refused at configuration time rather than at the point it would have drawn. `c64cast --compat` prints this at the terminal.

## The Matrix

| Overlay | `hires_edges` | `hires` | `mhires` | `mcm` | `petscii` | `blank` |
|---|---|---|---|---|---|---|
| `big_text` | · | · | · | ✓ | · | ✓ |
| `callsign` | ✓ | ✓ | ✓ | · | ✓ | ✓ |
| `clock` | ✓ | ✓ | ✓ | · | ✓ | ✓ |
| `countdown` | ✓ | ✓ | ✓ | · | ✓ | ✓ |
| `logo` | ✓ | ✓ | ✓ | · | ✓ | ✓ |
| `marquee` | ✓ | ✓ | ✓ | · | ✓ | ✓ |
| `network` | ✓ | ✓ | ✓ | · | ✓ | ✓ |
| `obs_status` | ✓ | ✓ | ✓ | · | ✓ | ✓ |
| `rss` | ✓ | ✓ | ✓ | · | ✓ | ✓ |
| `scrolling_text` | ✓ | ✓ | ✓ | · | ✓ | ✓ |
| `spectrum_bitmap` | · | · | ✓ | · | · | · |
| `spectrum_petscii` | · | · | · | · | ✓ | ✓ |
| `weather` | ✓ | ✓ | ✓ | · | ✓ | ✓ |

## Why a Cell Is Refused

A gap is one of three rules, not an accident of implementation. Text overlays need somewhere to put characters; a few overlays are written against one mode's memory layout; and an overlay that reads the audio stream needs the audio stream to exist.

| Overlay | Why it is unavailable |
|---|---|
| `big_text` | only on blank/mcm |
| `callsign` | needs a text-capable mode (petscii/blank/hires/mhires) |
| `clock` | needs a text-capable mode (petscii/blank/hires/mhires) |
| `countdown` | needs a text-capable mode (petscii/blank/hires/mhires) |
| `logo` | needs a text-capable mode (petscii/blank/hires/mhires) |
| `marquee` | needs a text-capable mode (petscii/blank/hires/mhires) |
| `network` | needs a text-capable mode (petscii/blank/hires/mhires) |
| `obs_status` | needs a text-capable mode (petscii/blank/hires/mhires) |
| `rss` | needs a text-capable mode (petscii/blank/hires/mhires) |
| `scrolling_text` | needs a text-capable mode (petscii/blank/hires/mhires) |
| `spectrum_bitmap` | only on mhires |
| `spectrum_petscii` | needs PETSCII-compatible mode (petscii/blank) |
| `weather` | needs a text-capable mode (petscii/blank/hires/mhires) |
