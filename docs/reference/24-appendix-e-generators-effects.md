---
number: E
generated: true
---

# Generators and Effects

*Generated from the code by `scripts/gen_reference_appendices.py`.
Edits here are overwritten; run `make reference-appendices`.*

The 20 procedural sources a `generative` scene can draw from, and the 8 effects that can be layered over any scene. Each entry lists what a knob can reach while the show is running under its name, spelled as the `target` a `param` mapping takes — so a line here can be copied into a `cc_map` unchanged. Appendix F is the same targets the other way round: one row each, with every generator or effect that declares it.

## Generators

Set one as a `generative` scene's `source`. Every generator renders at 320×200 and is downsampled by whichever display mode is in force.

<!-- table: fields -->
| Generator | Description |
|---|---|
| **`plasma`**<br>`source.speed` 0–2<br>`source.scale` 0.1–4 | Classic sine-sum plasma whose hue cycles over time. |
| **`tunnel`**<br>`source.speed` 0–2<br>`source.scale` 0.25–4 | Infinite-zoom tunnel: hue is driven by per-pixel depth (1/radius) and angle, scrolled over time. |
| **`fire`**<br>`source.scroll_speed` 0–4<br>`source.intensity` 0.2–2 | Rising fire: an upward-scrolling turbulence texture masked by a bottom-hot vertical gradient and colour-mapped black→red→yellow→white (`cv2.COLORMAP_HOT` — a near-perfect match for the C64 palette). |
| **`mandelbrot`**<br>`source.zoom_speed` 0.02–1<br>`source.cycle_speed` 0–2 | Escape-time Mandelbrot zoom. |
| **`moire2`**<br>`source.ring_freq` 10–80<br>`source.drift_speed` 0–2 | Two concentric-ring distance fields whose centers drift apart and together, summed into a classic moiré interference pattern (each field is `sin(distance-to-center * freq)`; xscreensaver's moire2.c gets the same beat pattern by XOR-compositing two arc bitmaps — this is the closed-form equivalent: a distance field instead of drawn arcs). |
| **`halo`**<br>`source.drift_speed` 0–2<br>`source.pulse_speed` 0–3 | Several soft-edged halos drifting on independent circular orbits, additively blended (bright where they overlap, no clear — matching xscreensaver's halo.c un-erased canvas). |
| **`epicycle`**<br>`source.speed` 0–2 | Fourier epicycles: a chain of circles, each spinning around the tip of the previous, whose combined tip traces `sum_i r_i * exp(j*(w_i t + phi_i))` — a chain of rotations composes to the same vector sum regardless of framing, so this sums phasors directly rather than nesting rotations. |
| **`hopalong`**<br>`source.a` -2–2<br>`source.drift_speed` 0–1 | Hopalong chaotic point-map attractor, iterated for many parallel starting points at once (numpy-vectorized across the batch — each *step* is still sequential, the map depends on the previous point) into a density accumulator, colour-mapped by (log-scaled) density. |
| **`rorschach`**<br>`source.grow_speed` 0–4 | Mirrored-symmetric ink-blot: a precomputed 2D random walk (fixed seed → deterministic) cumulative-summed from Gaussian steps, progressively revealed as `t` advances and reflected across the vertical center line — xscreensaver's rorschach.c animates the same way (draw a few more walk points each frame); this stays a pure function of `t` by redrawing however much of the (fixed) walk is "revealed" by `t` from scratch each frame, rather than accumulating pixels frame to frame. |
| **`hiphotic`**<br>`source.speed` 0.1–8<br>`source.scale` 0.1–4 | WLED "Hiphotic" port: nested trig interference (`sin(cos(x...) + sin(y...) + a)`), reimplemented in continuous float instead of WLED's 8-bit sin8/cos8 lookup tables. |
| **`metaballs`**<br>`source.speed` 0.05–5 | WLED "Metaballs" port: 3 moving "ball" centers blended into a classic inverse-distance metaball field. |
| **`rotozoomer`**<br>`source.speed` 0–4<br>`source.scale` 0.2–4 | WLED "Rotozoomer" port: a static XOR bit-pattern texture (`(x*4) ^ (y*4)`, precomputed + colorized once) sampled through a rotating/zooming affine transform. |
| **`lissajous`**<br>`source.speed` 0–4<br>`source.scale` 0.2–6 | WLED "Lissajous" port: a classic XY curve (`x = sin(theta*freq_x + phase)`, `y = cos(theta*2 + phase)`) sampled at a fixed number of points along its parametrization. |
| **`dna`**<br>`source.speed` 0–3<br>`source.scale` 0.3–4 | WLED "DNA" port: two sine strands sweeping the full frame width, phase-shifted by half a cycle (`pi`, matching WLED's `i*4` vs `i*4+128` offset) so they wind around a shared center line like a double helix; color cycles per column + time. |
| **`drift`**<br>`source.speed` 0–3<br>`source.scale` 0.3–2 | WLED "Drift" port: a rotating spiral trail — for radii `i` stepping outward from center, a point at angle `t*(maxDim-i)` traces a full spiral arm every frame. |
| **`colored_bursts`**<br>`source.speed` 0–3<br>`source.scale` 0.3–3 | WLED "Colored Bursts" port: several lines burst from one common, slowly-orbiting point out to per-line endpoints that trace their own faster orbits — WLED's shared start point has no per-line phase offset, while the per-line `i*24`/`i*48+64` phase spread on the *other* endpoint is what fans the lines out into a burst. |
| **`dotswarm`**<br>`source.speed` 0–3<br>`source.scale` 0.2–2 | A WLED "beatsin dot swarm" port covering the shared shape of several kin effects — Black Hole, Frizzles, Sindots, Squared Swirl, Drift Rose — which all boil down to the same primitive: a handful of points, each independently orbiting via a bounded sine (`beatsin8` in WLED) at its own frequency, color-cycled and blended. |
| **`game_of_life`**<br>`source.speed` 0.1–4 | WLED "Game Of Life" port: Conway's Game of Life on a coarse grid (chunky upscaled cells — reads great after C64 quantization, especially on PETSCII), with WLED's signature parent-color inheritance (a newly-born cell's hue is the mean of its live parents' hues). |
| **`soap`**<br>`source.speed` 0–3<br>`source.scale` 0.2–3 | WLED "Soap" port: a persistent color buffer smeared/advected each tick by a slowly-rotating noise-driven flow field — the classic swirling soap-film look. |
| **`fireworks`**<br>`source.speed` 0–3<br>`source.scale` 0.3–3 | WLED "Fireworks" port — the flagship of WLED's shared particle-system engine, which also drives Volcano/Ballpit/Waterfall/Impact/Attractor/ Galaxy as different emitter/gravity presets on the same primitive; only the fireworks preset is ported here. |

## Effects

Named by a scene's `effect`, or chained in order with `effects`. An effect transforms the frame after the source has drawn it and before the display mode quantises it.

<!-- table: fields -->
| Effect | Description |
|---|---|
| **`trails`**<br>`effect.decay` 0–0.96 | Feedback / echo trails: each frame is max-blended with a decayed copy of the previous output, so moving content leaves a fading comet tail. |
| **`pulse`**<br>`effect.intensity` 0–2.5 | Beat-punch zoom: a transient punches the frame scale up (zoom-in toward center), relaxing back as `onset` decays; sustained loudness adds a gentle steady zoom. |
| **`rgb_shift`**<br>`effect.intensity` 0–2.5 | Chromatic split: a transient slews the red and blue channels apart horizontally (opposite directions), an RGB-shift glitch shudder that snaps on the beat and relaxes as `onset` decays; loudness adds a steady split. |
| **`blur`**<br>`effect.intensity` 0–8 | Gaussian blur (`cv2.GaussianBlur`) — the first blur primitive in the codebase, added as an enabler for future dot/trail-family generator ports (WLED leans on `SEGMENT.blur` throughout its 2D effects). |
| **`strobe`**<br>`effect.duty` 0.05–1<br>`effect.rate` 1–16 | Tempo-locked strobe: blanks the frame to black for part of every beat, so the picture flashes on the grid. |
| **`invert`**<br>`effect.mix` 0–1 | Photo-negative: blends the frame toward its color inverse (`255 - px`). |
| **`mirror`**<br>`effect.axis` (3 values) | Symmetry fold: reflects one half of the frame onto the other, the classic VJ kaleidoscope-lite look. |
| **`posterize`**<br>`effect.levels` 2–32 | Level crush: quantizes each channel to `levels` steps, flattening the image into hard poster bands (a look that also pre-simplifies the frame for the C64 palette reduction downstream). |
