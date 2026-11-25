---
generated: true
---

# Live Targets

*Every parameter a `param` mapping can name.*

## Color pipeline

| `mode.` | Range | Declared by |
|---|---|---|
| `auto_fit_strength` | `0 – 1` | `mcm`, `mhires`, `petscii` |
| `background` | `16 values` | `blank` |
| `border` | `16 values` | `blank` |
| `cell_pick` | `2 values` | `hires` |
| `cell_strategy` | `4 values` | `mhires` |
| `color_match` | `2 values` | `hires`, `mcm`, `mhires`, `petscii` |
| `dither_method` | `5 values` | `hires`, `mcm`, `mhires` |
| `dither_strength` | `0 – 2` | `hires`, `mcm`, `mhires` |
| `motion_smoothing` | `0 – 1` | `mhires` |
| `palette_mode` | `4 values` | `mcm`, `mhires` |

## Effect

| `effect.` | Range | Declared by |
|---|---|---|
| `axis` | `3 values` | `mirror` |
| `decay` | `0 – 0.96` | `trails` |
| `duty` | `0.05 – 1` | `strobe` |
| `intensity` | `0 – 2.5` | `blur`, `pulse`, `rgb_shift` |
| `levels` | `2 – 32` | `posterize` |
| `mix` | `0 – 1` | `invert` |
| `rate` | `1 – 16` | `strobe` |

## Generator

| `source.` | Range | Declared by |
|---|---|---|
| `cycle_speed` | `0 – 2` | `mandelbrot` |
| `drift_speed` | `0 – 2` | `halo`, `hopalong`, `moire2` |
| `grow_speed` | `0 – 4` | `rorschach` |
| `intensity` | `0.2 – 2` | `fire` |
| `pulse_speed` | `0 – 3` | `halo` |
| `ring_freq` | `10 – 80` | `moire2` |
| `scale` | `0.1 – 4` | `colored_bursts`, `dna`, `dotswarm`, `drift`, `fireworks`, `hiphotic`, `lissajous`, `plasma`, `rotozoomer`, `soap`, `tunnel` |
| `scroll_speed` | `0 – 4` | `fire` |
| `shape` | `-2 – 2` | `hopalong` |
| `speed` | `0 – 2` | all but `fire`, `halo`, `hopalong`, `mandelbrot`, `moire2`, `rorschach` |
| `zoom_speed` | `0.02 – 1` | `mandelbrot` |

## Scope

| `scene.` | Range | Declared by |
|---|---|---|
| `gain` | `0.25 – 3` | `voice_scope` |
