---
number: H
generated: true
---

# Example Configurations

The 68 runnable configurations that ship inside the package. Run one with `c64cast --config example:NAME`, or copy it out to edit with `c64cast --print-example NAME > c64cast.toml`. Each summary is read from the file's own header comment.

## The Demos

A demo tagged *needs your own media* points at `assets/`, which ships empty because the material would be somebody else's. Drop a file in or repoint the scene's `file` before running it.

<!-- table: fields -->
| Name | Description |
|---|---|
| **`audio-reactive-input`** | generative visuals driven by LIVE AUDIO INPUT. |
| **`audio-reactive-listen`** | generative visuals driven by LIVE AUDIO INPUT, with NO C64 audio output — the "listen-only" VJ case. |
| **`c64cast.example`** | c64cast config — annotated reference + feature showcase. |
| **`color-dither`** | spatial dither ([color].dither) on a slideshow. *(needs your own media)* |
| **`color-force-palette`** | the EXTREME forced-palette remap ([color].force_palette). *(needs your own media)* |
| **`effect-chain`** | Layerable effect chain demo (Live DJ/VJ Phase 3): stack several pixel effects on one scene, each independently tunable and bypass-toggleable live, … |
| **`effect-reactive`** | a MUSIC-REACTIVE pixel effect over a generative source + SID-file playback. |
| **`effect-trails`** | a per-scene pixel EFFECT layered on a generative source. |
| **`hello`** | c64cast "hello world" — the simplest thing that puts something on the screen. |
| **`menu`** | the on-C64 menu. |
| **`overlay-big_text`** | blank canvas + big_text overlay. |
| **`overlay-callsign`** | PETSCII webcam + callsign overlay. |
| **`overlay-clock`** | PETSCII webcam + clock overlay. |
| **`overlay-countdown`** | PETSCII webcam + countdown overlay. |
| **`overlay-logo`** | PETSCII webcam + logo overlay. |
| **`overlay-marquee`** | PETSCII webcam + marquee overlay. |
| **`overlay-network`** | PETSCII webcam + network overlay. |
| **`overlay-obs_status`** | PETSCII webcam + OBS Studio status overlay. |
| **`overlay-rss`** | PETSCII webcam + RSS feed ticker. |
| **`overlay-scrolling_text`** | PETSCII webcam + scrolling_text overlay. |
| **`overlay-spectrum_bitmap`** | multicolor-bitmap spectrum analyzer over a generative plasma, driven by live audio input. |
| **`overlay-spectrum_petscii`** | PETSCII webcam + audio spectrum analyzer. |
| **`overlay-weather`** | PETSCII webcam + weather overlay. |
| **`performance-clips`** | Clip-launch grid demo (Live DJ/VJ Phase 2): fire scenes from a MIDI controller, quantized to a musical beat grid — a "video sampler" in the spirit of … |
| **`scene-asid`** | ASID stream → real SID + oscilloscope. |
| **`scene-blank`** | blank PETSCII canvas with a big_text overlay. |
| **`scene-generative-colored-bursts`** | "Colored Bursts" — a WLED-effect port. |
| **`scene-generative-dna`** | "DNA" — a WLED-effect port. |
| **`scene-generative-dotswarm`** | "Dot Swarm" — a WLED-effect port covering the shared shape of several kin effects: a handful of points, each independently orbiting via a bounded … |
| **`scene-generative-drift`** | "Drift" — a WLED-effect port. |
| **`scene-generative-epicycle`** | a Fourier epicycle chain (circles spinning around the tip of the previous circle) rendered as a multicolor bitmap. |
| **`scene-generative-fire`** | a generative FIRE source paired with SID-file playback — the most viscerally music-reactive generator. |
| **`scene-generative-fireworks`** | "Fireworks" — a WLED-effect port of WLED's shared particle-system engine's flagship preset: shells launch, arc under gravity, and explode into a … |
| **`scene-generative-game-of-life`** | "Game Of Life" — a WLED-effect port of Conway's Game of Life on a coarse grid, with WLED's signature "parent color inheritance": a newly-born cell's … |
| **`scene-generative-halo`** | several soft-edged halos drifting on independent orbits, additively blended (bright where they overlap), rendered as a multicolor bitmap. |
| **`scene-generative-hiphotic`** | "Hiphotic" — a WLED-effect port. |
| **`scene-generative-hopalong`** | the Hopalong chaotic point-map attractor (Barry Martin's `x' = y - sign(x)*sqrt(\|b*x - c\|)`, `y' = a - x`) rendered as a multicolor bitmap. |
| **`scene-generative-lissajous`** | "Lissajous" — a WLED-effect port. |
| **`scene-generative-mandelbrot`** | a procedural Mandelbrot zoom rendered as a multicolor bitmap. |
| **`scene-generative-metaballs`** | "Metaballs" — a WLED-effect port. |
| **`scene-generative-moire2`** | a moiré interference pattern rendered as a multicolor bitmap. |
| **`scene-generative-petscii`** | the SAME plasma source as scene-generative-plasma.toml, but rendered as PETSCII glyphs instead of a multicolor bitmap. |
| **`scene-generative-plasma`** | a procedural plasma rendered as a multicolor bitmap. |
| **`scene-generative-rorschach`** | a mirrored-symmetric ink-blot rendered as a multicolor bitmap. |
| **`scene-generative-rotozoomer`** | "Rotozoomer" — a WLED-effect port. |
| **`scene-generative-sid`** | a generative plasma paired with SID-file playback — the headline of the composable building blocks. |
| **`scene-generative-soap`** | "Soap" — a WLED-effect port of a persistent color buffer smeared/advected each tick by a slowly-rotating noise-driven flow field — the classic … |
| **`scene-launcher`** | launch a native C64 program (game or demo) on the U64 and hand the machine over to it. *(needs your own media)* |
| **`scene-midi`** | MIDI → SID synth + oscilloscope. |
| **`scene-slideshow`** | cycle through still images on the C64 display. *(needs your own media)* |
| **`scene-video-sampler`** | video playback with HIGH-FIDELITY audio via the U64's "Ultimate Audio" FPGA PCM sampler (instead of the lo-fi 4-bit $D418 DAC). *(needs your own media)* |
| **`scene-video`** | video file ("video") playback with audio. *(needs your own media)* |
| **`scene-waveform`** | SID playback ("waveform" scene). *(needs your own media)* |
| **`scene-webcam-audio`** | webcam → PETSCII + live mic through the SID DAC. |
| **`scene-webcam-hires`** | webcam → hi-res bitmap mode. |
| **`scene-webcam-hires_edges`** | webcam → hi-res bitmap with Canny edge detection. |
| **`scene-webcam-mcm`** | webcam → multicolor character mode (MCM). |
| **`scene-webcam-mhires`** | webcam → multicolor hi-res bitmap (MHires). |
| **`scene-webcam-petscii`** | webcam → PETSCII char mode. |
| **`scene-wled`** | the C64 as a virtual WLED LED matrix (WLED bridge Mode 2). |
| **`teensyrom-blank`** | the TeensyROM+ backend driving a blank canvas with a scrolling-text overlay over USB serial. |
| **`vision-gesture`** | webcam hand-gesture control (the vision controller). |
| **`vision-modes`** | Vision demo: SWIPE cycles the video MODE; hold-open cycles the style within a mode; pinch pauses. |
| **`wled-control`** | Control c64cast FROM the WLED app (WLED bridge Mode 1 — "listen"). *(needs your own media)* |
| **`ensemble/left`** | Per-system config for the leftmost screen in the ensemble. |
| **`ensemble/master`** | Master config for a 3-system ensemble (a row of three Ultimate 64s laid out left → middle → right, viewed from the front). |
| **`ensemble/middle`** | Per-system config for the middle screen. |
| **`ensemble/right`** | Per-system config for the rightmost screen — also the *conductor* for the cross-system big_text broadcast (since the message scrolls right-to-left, … |
