---
generated: true
---

# Live Targets

*Every parameter a `param` mapping can name.*

## Color pipeline

| `mode.` | Range | Declared by |
|---|---|---|
| `auto_fit_strength` | `0 – 1` | `petscii`, `mcm`, `mhires` |
| `color_match` | `2 values` | `petscii`, `mcm`, `hires`, `mhires` |
| `border` | `16 values` | `blank` |
| `background` | `16 values` | `blank` |
| `dither_strength` | `0 – 2` | `mcm`, `hires`, `mhires` |
| `dither_method` | `5 values` | `mcm`, `hires`, `mhires` |
| `palette_mode` | `4 values` | `mcm`, `mhires` |
| `cell_pick` | `2 values` | `hires` |
| `motion_smoothing` | `0 – 1` | `mhires` |
| `cell_strategy` | `4 values` | `mhires` |

## Effect

| `effect.` | Range | Declared by |
|---|---|---|
| `decay` | `0 – 0.96` | `trails` |
| `intensity` | `0 – 2.5` | `pulse`, `rgb_shift`, `blur` |
| `duty` | `0.05 – 1` | `strobe` |
| `rate` | `1 – 16` | `strobe` |
| `mix` | `0 – 1` | `invert` |
| `axis` | `3 values` | `mirror` |
| `levels` | `2 – 32` | `posterize` |

## Generator

| `source.` | Range | Declared by |
|---|---|---|
| `speed` | `0 – 2` | all but `fire`, `mandelbrot`, `moire2`, `halo`, `hopalong`, `rorschach` |
| `scale` | `0.1 – 4` | `plasma`, `tunnel`, `hiphotic`, `rotozoomer`, `lissajous`, `dna`, `drift`, `colored_bursts`, `dotswarm`, `soap`, `fireworks` |
| `scroll_speed` | `0 – 4` | `fire` |
| `intensity` | `0.2 – 2` | `fire` |
| `zoom_speed` | `0.02 – 1` | `mandelbrot` |
| `cycle_speed` | `0 – 2` | `mandelbrot` |
| `ring_freq` | `10 – 80` | `moire2` |
| `drift_speed` | `0 – 2` | `moire2`, `halo`, `hopalong` |
| `pulse_speed` | `0 – 3` | `halo` |
| `shape` | `-2 – 2` | `hopalong` |
| `grow_speed` | `0 – 4` | `rorschach` |

## Scope

| `scene.` | Range | Declared by |
|---|---|---|
| `gain` | `0.25 – 3` | `voice_scope` |
