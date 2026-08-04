---
number: D
generated: true
---

# Overlay and Display-Mode Compatibility

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

| Rule | Overlays |
|---|---|
| Only on blank/mcm | `big_text` |
| Needs a text-capable mode (petscii/blank/hires/mhires) | `callsign`, `clock`, `countdown`, `logo`, `marquee`, `network`, `obs_status`, `rss`, `scrolling_text`, `weather` |
| Only on mhires | `spectrum_bitmap` |
| Needs PETSCII-compatible mode (petscii/blank) | `spectrum_petscii` |
