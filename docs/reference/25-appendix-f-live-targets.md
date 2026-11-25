---
number: F
generated: true
---

# Live-Tune Targets

The 29 parameters a MIDI knob, pad or web-console control can move while a show is running. Each names the `target` of a `param` action in `[[midi_control.cc_map]]`: the holder that heads its section, a dot, and the parameter. A knob sweeps a scalar or bucket-selects a choice; a pad steps a choice on.

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
| `auto_fit_strength` | `scalar` | `0 – 1` | `mcm`, `mhires`, `petscii` |
| `background` | `choice` | `black`, `white`, `red`, `cyan`, `purple`, `green`, `blue`, `yellow`, `orange`, `brown`, `light red`, `dark gray`, `gray`, `light green`, `light blue`, `light gray` | `blank` |
| `border` | `choice` | `black`, `white`, `red`, `cyan`, `purple`, `green`, `blue`, `yellow`, `orange`, `brown`, `light red`, `dark gray`, `gray`, `light green`, `light blue`, `light gray` | `blank` |
| `cell_pick` | `choice` | `error-min`, `sample` | `hires` |
| `cell_strategy` | `choice` | `frequency`, `luminance`, `contrast`, `error-min` | `mhires` |
| `color_match` | `choice` | `rgb`, `perceptual` | `hires`, `mcm`, `mhires`, `petscii` |
| `dither_method` | `choice` | `none`, `ordered`, `blue_noise`, `floyd-steinberg`, `atkinson` | `hires`, `mcm`, `mhires` |
| `dither_strength` | `scalar` | `0 – 2` | `hires`, `mcm`, `mhires` |
| `motion_smoothing` | `scalar` | `0 – 1` | `mhires` |
| `palette_mode` | `choice` | `percell`, `cheap`, `vivid`, `grayscale` | `mcm`, `mhires` |

## `effect`

An effect in the scene's chain. A row's target is `effect.` and its name.

| Parameter | Kind | Range or values | Declared by |
|---|---|---|---|
| `axis` | `choice` | `horizontal`, `vertical`, `quad` | `mirror` |
| `decay` | `scalar` | `0 – 0.96` | `trails` |
| `duty` | `scalar` | `0.05 – 1` | `strobe` |
| `intensity` | `scalar` | `0 – 2.5` | `blur`, `pulse`, `rgb_shift` |
| `levels` | `scalar` | `2 – 32` | `posterize` |
| `mix` | `scalar` | `0 – 1` | `invert` |
| `rate` | `scalar` | `1 – 16` | `strobe` |

## `source`

A generative scene's generator. A row's target is `source.` and its name.

| Parameter | Kind | Range or values | Declared by |
|---|---|---|---|
| `cycle_speed` | `scalar` | `0 – 2` | `mandelbrot` |
| `drift_speed` | `scalar` | `0 – 2` | `halo`, `hopalong`, `moire2` |
| `grow_speed` | `scalar` | `0 – 4` | `rorschach` |
| `intensity` | `scalar` | `0.2 – 2` | `fire` |
| `pulse_speed` | `scalar` | `0 – 3` | `halo` |
| `ring_freq` | `scalar` | `10 – 80` | `moire2` |
| `scale` | `scalar` | `0.1 – 4` | `colored_bursts`, `dna`, `dotswarm`, `drift`, `fireworks`, `hiphotic`, `lissajous`, `plasma`, `rotozoomer`, `soap`, `tunnel` |
| `scroll_speed` | `scalar` | `0 – 4` | `fire` |
| `shape` | `scalar` | `-2 – 2` | `hopalong` |
| `speed` | `scalar` | `0 – 2` | `colored_bursts`, `dna`, `dotswarm`, `drift`, `epicycle`, `fireworks`, `game_of_life`, `hiphotic`, `lissajous`, `metaballs`, `plasma`, `rotozoomer`, `soap`, `tunnel` |
| `zoom_speed` | `scalar` | `0.02 – 1` | `mandelbrot` |

## `scene`

The scene itself. A row's target is `scene.` and its name.

| Parameter | Kind | Range or values | Declared by |
|---|---|---|---|
| `gain` | `scalar` | `0.25 – 3` | `voice_scope` |
