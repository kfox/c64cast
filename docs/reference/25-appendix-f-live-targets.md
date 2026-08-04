---
number: F
generated: true
---

# Live-Tune Targets

The 26 parameters a MIDI knob, pad or web-console control can move while a show is running. Each names the `target` of a `param` action in `[[midi_control.cc_map]]`: the holder that heads its section, a dot, and the parameter. A knob sweeps a scalar or bucket-selects a choice; a pad steps a choice on.

## Mapping One

```toml
[[midi_control.cc_map]]
type = "cc"              # cc | note | pc
number = 13              # the controller number
action = "param"
target = "effect.decay"  # a heading and a row below
```

A target is only live while something that declares it is on screen — the *Declared by* column is that list. A knob on a target the running scene does not declare moves nothing, silently.

## `mode`

The display mode's color pipeline. A row's target is `mode.` and its name.

| Parameter | Kind | Range or values | Declared by |
|---|---|---|---|
| `auto_fit_strength` | `scalar` | `0 – 1` | `petscii`, `mcm`, `mhires` |
| `color_match` | `choice` | `rgb`, `perceptual` | `petscii`, `mcm`, `hires`, `mhires` |
| `dither_strength` | `scalar` | `0 – 2` | `mcm`, `hires`, `mhires` |
| `dither_method` | `choice` | `none`, `ordered`, `blue_noise`, `floyd-steinberg`, `atkinson` | `mcm`, `hires`, `mhires` |
| `palette_mode` | `choice` | `percell`, `cheap`, `vivid`, `grayscale` | `mcm`, `mhires` |
| `motion_smoothing` | `scalar` | `0 – 1` | `mhires` |
| `cell_strategy` | `choice` | `frequency`, `luminance`, `contrast`, `error-min` | `mhires` |

## `effect`

An effect in the scene's chain. A row's target is `effect.` and its name.

| Parameter | Kind | Range or values | Declared by |
|---|---|---|---|
| `decay` | `scalar` | `0 – 0.96` | `trails` |
| `intensity` | `scalar` | `0 – 2.5` | `pulse`, `rgb_shift`, `blur` |
| `duty` | `scalar` | `0.05 – 1` | `strobe` |
| `rate` | `scalar` | `1 – 16` | `strobe` |
| `mix` | `scalar` | `0 – 1` | `invert` |
| `axis` | `choice` | `horizontal`, `vertical`, `quad` | `mirror` |
| `levels` | `scalar` | `2 – 32` | `posterize` |

## `source`

A generative scene's generator. A row's target is `source.` and its name.

| Parameter | Kind | Range or values | Declared by |
|---|---|---|---|
| `speed` | `scalar` | `0 – 2` | `plasma`, `tunnel`, `epicycle`, `hiphotic`, `metaballs`, `rotozoomer`, `lissajous`, `dna`, `drift`, `colored_bursts`, `dotswarm`, `game_of_life`, `soap`, `fireworks` |
| `scale` | `scalar` | `0.1 – 4` | `plasma`, `tunnel`, `hiphotic`, `rotozoomer`, `lissajous`, `dna`, `drift`, `colored_bursts`, `dotswarm`, `soap`, `fireworks` |
| `scroll_speed` | `scalar` | `0 – 4` | `fire` |
| `intensity` | `scalar` | `0.2 – 2` | `fire` |
| `zoom_speed` | `scalar` | `0.02 – 1` | `mandelbrot` |
| `cycle_speed` | `scalar` | `0 – 2` | `mandelbrot` |
| `ring_freq` | `scalar` | `10 – 80` | `moire2` |
| `drift_speed` | `scalar` | `0 – 2` | `moire2`, `halo`, `hopalong` |
| `pulse_speed` | `scalar` | `0 – 3` | `halo` |
| `a` | `scalar` | `-2 – 2` | `hopalong` |
| `grow_speed` | `scalar` | `0 – 4` | `rorschach` |

## `scene`

The scene itself. A row's target is `scene.` and its name.

| Parameter | Kind | Range or values | Declared by |
|---|---|---|---|
| `gain` | `scalar` | `0.25 – 3` | `voice_scope` |
