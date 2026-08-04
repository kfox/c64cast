---
number: F
generated: true
---

# Live-Tune Targets

The 26 parameters a MIDI knob, pad or web-console control can move while a show is running. Each is the `target` string of a `param` action in `[[midi_control.cc_map]]`. A knob sweeps a scalar or bucket-selects a choice; a pad steps a choice on.

## Mapping One

```toml
[[midi_control.cc_map]]
type = "cc"              # cc | note | pc
number = 13              # the controller number
action = "param"
target = "effect.decay"  # a row below
```

A target is only live while something that declares it is on screen — the *Declared by* column is that list. A knob on a target the running scene does not declare moves nothing, silently.

## Color pipeline

| Target | Kind | Range or values | Declared by |
|---|---|---|---|
| `mode.auto_fit_strength` | `scalar` | `0 – 1` | `petscii`, `mcm`, `mhires` |
| `mode.color_match` | `choice` | `rgb`, `perceptual` | `petscii`, `mcm`, `hires`, `mhires` |
| `mode.dither_strength` | `scalar` | `0 – 2` | `mcm`, `hires`, `mhires` |
| `mode.dither_method` | `choice` | `none`, `ordered`, `blue_noise`, `floyd-steinberg`, `atkinson` | `mcm`, `hires`, `mhires` |
| `mode.palette_mode` | `choice` | `percell`, `cheap`, `vivid`, `grayscale` | `mcm`, `mhires` |
| `mode.motion_smoothing` | `scalar` | `0 – 1` | `mhires` |
| `mode.cell_strategy` | `choice` | `frequency`, `luminance`, `contrast`, `error-min` | `mhires` |

## Effect

| Target | Kind | Range or values | Declared by |
|---|---|---|---|
| `effect.decay` | `scalar` | `0 – 0.96` | `trails` |
| `effect.intensity` | `scalar` | `0 – 2.5` | `pulse`, `rgb_shift`, `blur` |
| `effect.duty` | `scalar` | `0.05 – 1` | `strobe` |
| `effect.rate` | `scalar` | `1 – 16` | `strobe` |
| `effect.mix` | `scalar` | `0 – 1` | `invert` |
| `effect.axis` | `choice` | `horizontal`, `vertical`, `quad` | `mirror` |
| `effect.levels` | `scalar` | `2 – 32` | `posterize` |

## Generator

| Target | Kind | Range or values | Declared by |
|---|---|---|---|
| `source.speed` | `scalar` | `0 – 2` | `plasma`, `tunnel`, `epicycle`, `hiphotic`, `metaballs`, `rotozoomer`, `lissajous`, `dna`, `drift`, `colored_bursts`, `dotswarm`, `game_of_life`, `soap`, `fireworks` |
| `source.scale` | `scalar` | `0.1 – 4` | `plasma`, `tunnel`, `hiphotic`, `rotozoomer`, `lissajous`, `dna`, `drift`, `colored_bursts`, `dotswarm`, `soap`, `fireworks` |
| `source.scroll_speed` | `scalar` | `0 – 4` | `fire` |
| `source.intensity` | `scalar` | `0.2 – 2` | `fire` |
| `source.zoom_speed` | `scalar` | `0.02 – 1` | `mandelbrot` |
| `source.cycle_speed` | `scalar` | `0 – 2` | `mandelbrot` |
| `source.ring_freq` | `scalar` | `10 – 80` | `moire2` |
| `source.drift_speed` | `scalar` | `0 – 2` | `moire2`, `halo`, `hopalong` |
| `source.pulse_speed` | `scalar` | `0 – 3` | `halo` |
| `source.a` | `scalar` | `-2 – 2` | `hopalong` |
| `source.grow_speed` | `scalar` | `0 – 4` | `rorschach` |

## Scope

| Target | Kind | Range or values | Declared by |
|---|---|---|---|
| `scene.gain` | `scalar` | `0.25 – 3` | `voice_scope` |
