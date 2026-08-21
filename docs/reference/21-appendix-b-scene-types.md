---
number: B
generated: true
---

# Scene Types

The 10 kinds of scene a `[[scenes]]` block can be, in alphabetical order, and the keys each one reads. A key marked *live-tunable* can be moved by a knob mid-show; one marked *menu-live* is one the on-C64 menu can change without rebuilding the scene. `c64cast --describe scene:NAME` prints any one of these at the terminal.

## Keys Every Scene Takes

These apply whatever the scene's `type` is. The per-type sections below list only what is particular to that type.

<!-- table: fields -->
| Key | Description |
|---|---|
| **`type`**<br>*Type:* `str`<br>*Default:* `'webcam'` | Scene kind. Choices: `webcam`, `blank`, `video`, `waveform`, `midi`, `asid`, `slideshow`, `launcher`, `generative`, `wled`. |
| **`name`**<br>*Type:* `str \| None`<br>*Default:* `None` | Display name (shown in interstitials/logs; ensemble match key). |
| **`target_fps`**<br>*Type:* `float \| None`<br>*Default:* `None` | Per-scene frame-rate cap; unset = playlist default (60/50). Bitmap (hires/mhires) video/webcam/generative scenes default lower to stay under the DMA bus-halt ceiling: 20 fps while streaming digitized audio, else half rate (30/25). Generative and webcam scenes take that 20 fps cap in CHAR modes too whenever audio is on the 4-bit DAC — they repaint every tick (no dedup), so the frame writes contend with the audio ring for the DMA socket. Off-bus Ultimate Audio sampler playback keeps the high default. Waveform/midi/asid default to half rate too. *Menu-live*: the on-C64 menu offers this knob, applied to the running scene. |
| **`overlays`**<br>*Type:* `list[dict[str, Any]]`<br>*Default:* `[]` | List of overlay tables ([[scenes.overlays]]); see `--list-overlays`. |
| **`orchestrate`**<br>*Type:* `bool`<br>*Default:* `False` | Ensemble: make this system the conductor and broadcast this scene to all others (requires name; ignored single-system). |
| **`follower_only`**<br>*Type:* `bool`<br>*Default:* `False` | Ensemble: exclude from normal rotation; used only as a broadcast follower override (requires name; excludes orchestrate). |

Every type but `video` takes these as well.

<!-- table: fields -->
| Key | Description |
|---|---|
| **`duration_s`**<br>*Type:* `float \| None`<br>*Default:* `None` | Seconds before auto-advance; 0 = run forever. Unset = scene-type default (webcam/blank run forever when they're the only scene, else 30s; waveform = song length or 30s; slideshow/generative = 30s). Video scenes reject this (they run until the file ends). For launcher this is the idle timeout (reset by player input). *Menu-live*: the on-C64 menu offers this knob, applied to the running scene. |

## `asid`

Play an incoming ASID MIDI stream on the real SID + 3-voice oscilloscope (bitmap-only).

```toml
[[scenes]]
type = "asid"
color_mode = "per_voice"  # per_voice | per_waveform
time_base = "wallclock"   # wallclock | auto
auto_cycles = 4
```

<!-- table: fields -->
| Key | Description |
|---|---|
| **`color_mode`**<br>*Type:* `str`<br>*Default:* `'per_voice'` | Oscilloscope coloring: fixed per voice, or by current waveform type. Choices: `per_voice`, `per_waveform`. |
| **`voice_colors`**<br>*Type:* `list[str]`<br>*Default:* `[]` | Per-voice trace colors (C64 color names) for color_mode=per_voice. |
| **`waveform_colors`**<br>*Type:* `dict[str, str]`<br>*Default:* `{}` | Per-waveform-type colors (e.g. pulse=cyan) for color_mode=per_waveform. |
| **`time_base`**<br>*Type:* `str`<br>*Default:* `'wallclock'` | Scope time window: 'wallclock' (1 row = 1 frame) or 'auto' (per-voice window sized so auto_cycles cycles fit). Choices: `wallclock`, `auto`. |
| **`auto_cycles`**<br>*Type:* `float`<br>*Default:* `4.0` | Complete cycles per render window when time_base = 'auto'. |
| **`persistence`**<br>*Type:* `str`<br>*Default:* `'off'` | Trace decay/trail length ('off' redraws each frame). Choices: `off`, `short`, `medium`, `long`, `random`. |
| **`scroll_columns`**<br>*Type:* `int \| list[int]`<br>*Default:* `0` | FIFO-scroll the strip left by N columns/frame (0 = redraw). Int or a list of 3 per-voice ints. |
| **`asid_port`**<br>*Type:* `str \| None`<br>*Default:* `None` | MIDI input port name substring the ASID host streams to; unset = first available port. |
| **`asid_multi_sid`**<br>*Type:* `bool`<br>*Default:* `True` | Honor ASID multi-SID streams (commands 0x50-0x5F) by configuring the U64 for multiple SIDs and routing each chip to its own address (prefers physical socket SIDs). U64 only — ignored on backends without the config API, where extra chips downmix to the primary SID. |
| **`asid_max_sids`**<br>*Type:* `int`<br>*Default:* `8` | Cap on the number of SID chips a multi-SID ASID stream may map on the U64 (1-8). Chips beyond the cap downmix to the primary SID. |
| **`asid_buffered_player`**<br>*Type:* `str`<br>*Default:* `'auto'` | Cycle-accurate buffered playback: consume ASID frames on a C64-side REU ring player (CIA #1 Timer A IRQ) instead of coalescing block writes on the host. Fixes dropped frames on multispeed tunes (0x31 up to 16x) — arps/vibrato/hard restarts survive — and honors the 0x30 write-order/wait recipe. U64 only (needs a bus-clean REU): 'auto' = on when the backend has an REU, else the coalesced path; 'on' = force it (warns + falls back on a no-REU backend); 'off' = always coalesce. Choices: `auto`, `on`, `off`. |

## `blank`

Empty canvas (no video) — a foundation for overlays.

Display modes: `blank`, `hires_edges`.

```toml
[[scenes]]
type = "blank"
border = 0
background = 0
```

<!-- table: fields -->
| Key | Description |
|---|---|
| **`display`**<br>*Type:* `str \| None`<br>*Default:* `None` | VIC-II display mode. Unset resolves per scene type: 'mhires' for video (richest bitmap mode, suits arbitrary film/photo content) and 'hires_edges' for webcam/blank/slideshow/generative (tuned for live Canny-edge stylization). waveform and midi are bitmap-only (both ignore this); slideshow also accepts 'random'. generative renders a frame so any quantizing mode works (not 'blank'/'random'). Choices: `hires_edges`, `hires`, `petscii`, `mcm`, `mhires`, `blank`, `random`. |
| **`audio`**<br>*Type:* `bool \| None`<br>*Default:* `None` | Per-scene audio override. Unset follows [audio].enabled; false mutes this scene only. |
| **`pre_emphasis`**<br>*Type:* `float \| None`<br>*Default:* `None` | Per-scene HF pre-emphasis (0 = off, ~0.3-0.7 typical; brightens speech). Unset = global [dsp].pre_emphasis / source-aware default. Needs [dsp].enabled + scene audio. |
| **`border`**<br>*Type:* `int \| str`<br>*Default:* `0` | Border color (blank scenes): a C64 color name (fuzzy + case-insensitive, e.g. "light blue") or a palette index 0..15. |
| **`background`**<br>*Type:* `int \| str`<br>*Default:* `0` | Background color (blank scenes): a C64 color name (fuzzy + case-insensitive, e.g. "light blue") or a palette index 0..15. |

## `generative`

Procedural video (plasma/tunnel/…) rendered to any display mode.

Display modes: `mhires`, `hires`, `hires_edges`, `mcm`, `petscii`.

```toml
[[scenes]]
type = "generative"
source = "plasma"
audio_source = "none"  # none | mic | listen | file | sid
reactive = true
```

<!-- table: fields -->
| Key | Description |
|---|---|
| **`display`**<br>*Type:* `str \| None`<br>*Default:* `None` | VIC-II display mode. Unset resolves per scene type: 'mhires' for video (richest bitmap mode, suits arbitrary film/photo content) and 'hires_edges' for webcam/blank/slideshow/generative (tuned for live Canny-edge stylization). waveform and midi are bitmap-only (both ignore this); slideshow also accepts 'random'. generative renders a frame so any quantizing mode works (not 'blank'/'random'). Choices: `hires_edges`, `hires`, `petscii`, `mcm`, `mhires`, `blank`, `random`. |
| **`file`**<br>*Type:* `str \| None`<br>*Default:* `None` | Asset spec (comma-separated paths/dirs/globs). Videos for video, .sid for waveform, images for slideshow, .prg/.crt for launcher, .sid for generative when audio_source = sid. |
| **`audio`**<br>*Type:* `bool \| None`<br>*Default:* `None` | Per-scene audio override. Unset follows [audio].enabled; false mutes this scene only. |
| **`source`**<br>*Type:* `str`<br>*Default:* `'plasma'` | Generative video source to render (generative scenes only). Choices: `plasma`, `tunnel`, `fire`, `mandelbrot`, `moire2`, `halo`, `epicycle`, `hopalong`, `rorschach`, `hiphotic`, `metaballs`, `rotozoomer`, `lissajous`, `dna`, `drift`, `colored_bursts`, `dotswarm`, `game_of_life`, `soap`, `fireworks`. |
| **`audio_source`**<br>*Type:* `str`<br>*Default:* `'none'` | Audio for a generative scene: 'none' = silent (default); 'mic' = live audio input — an instrument/mixer feed via an interface, or a mic — streamed to the 4-bit DAC and analyzed; 'listen' = analyze the live input for reactive visuals ONLY, with no C64 audio output (the VJ case: the real sound is on a PA and only the visuals track it — and, freed from the DAC rate, it captures full-bandwidth at [audio_features].listen_sample_rate); 'sid' = play the `file` .sid on the real chip. 'mic'/'listen' need [audio].enabled for the capture subsystem. 'mic', 'listen' and 'sid' all drive reactive visuals (see `reactive`); the input analyzer is tunable under [audio_features]. A SID source forces a host-DMA display and needs a char display (petscii/mcm) for most tunes (see `file`). Choices: `none`, `mic`, `listen`, `file`, `sid`. |
| **`reactive`**<br>*Type:* `bool`<br>*Default:* `True` | Generative scene: let the music drive the visuals — BPM cycles the colors, transients pulse them, bass reads differently from treble. Works with audio_source = 'sid' (a host-side SID emulator supplies the features, adding no U64 traffic) and 'mic'/'listen' (the live input is analyzed on the host — see [audio_features]); inert for 'none'. Set false to keep the pure time-driven look (and, for 'listen', to skip capture entirely). |
| **`effect`**<br>*Type:* `str \| None`<br>*Default:* `None` | Pixel effect applied to the frame before quantization (unset = none). Works on any frame-bearing scene. 'trails' echoes moving content; 'pulse' beat-punches the zoom; 'rgb_shift' slews the color channels apart on a transient. pulse/rgb_shift only visibly react on a music-reactive scene (generative + audio_source = 'sid'); elsewhere they're inert (no feature stream to react to). Choices: `trails`, `pulse`, `rgb_shift`, `blur`, `strobe`, `invert`, `mirror`, `posterize`. |
| **`effects`**<br>*Type:* `list[str]`<br>*Default:* `[]` | Ordered pixel-effect chain applied before quantization, e.g. effects = ["trails", "rgb_shift", "strobe"]. Each is one of the `effect` choices; layers apply in order and are individually tunable (map a CC to fx0.<param>/fx1.<param>…) and bypass-toggleable live (fx_toggle). Mutually exclusive with the single `effect` field. Empty = none. Choices: `trails`, `pulse`, `rgb_shift`, `blur`, `strobe`, `invert`, `mirror`, `posterize`. |
| **`mod_source`**<br>*Type:* `str`<br>*Default:* `'audio'` | What drives this scene's reactive effect layers: 'audio' (the SID feature stream — needs a music-reactive scene, i.e. generative + audio_source = 'sid'), 'clock' (the [performance] beat grid, so effects lock to MIDI/tap tempo on any scene — the way to tempo-lock a 'strobe'), or 'off' (never react — layers use their static baseline). Applies to every effect layer on the scene. Choices: `audio`, `clock`, `off`. |
| **`pre_emphasis`**<br>*Type:* `float \| None`<br>*Default:* `None` | Per-scene HF pre-emphasis (0 = off, ~0.3-0.7 typical; brightens speech). Unset = global [dsp].pre_emphasis / source-aware default. Needs [dsp].enabled + scene audio. |
| **`song`**<br>*Type:* `int`<br>*Default:* `0` | SID subtune index to play (0 = the SID's default; 1-based otherwise). For generative scenes, only with audio_source = sid. |
| **`palette_mode`**<br>*Type:* `str`<br>*Default:* `'percell'` | VIC-II slot-allocation strategy for mcm/mhires display (ignored by other modes): percell (default), cheap, vivid, grayscale. Color shaping (channel boost + hue corrections, e.g. the purple rescue) is the global [color] section, applied to every mode. Choices: `percell`, `cheap`, `vivid`, `grayscale`. *Live-tunable* while a show runs, as `mode.palette_mode` — Appendix F. *Menu-live*: the on-C64 menu offers this knob, applied to the running scene. |
| **`text_double_height`**<br>*Type:* `bool`<br>*Default:* `False` | On mhires, render text overlays (clock/marquee/…) at double height — 16px / 2 cell rows — for across-the-room legibility. Text is always double-WIDE on mhires (8x8 glyph spans 2 of the 4px cells); this toggle adds the vertical stretch. Ignored on other display modes. |
| **`style`**<br>*Type:* `str`<br>*Default:* `'default'` | PETSCII glyph/color style (only when display = 'petscii'); 'random' picks one at setup. Choices: `default`, `halftone`, `random_glyph`, `letter_rain`, `neon`, `inverse_pop`, `hatch`, `color_only`, `random`. *Menu-live*: the on-C64 menu offers this knob, applied to the running scene. |
| **`color`**<br>*Type:* `dict[str, Any]`<br>*Default:* `{}` | Per-scene [color] override ([scenes.color] sub-table): any [color] field set here replaces the global value for this scene only. Unset fields follow the global [color] section. See `--describe color` for the field list. |

## `launcher`

Launch a native C64 program (.prg/.crt) and hand the machine over; idle timeout resets on player input.

```toml
[[scenes]]
type = "launcher"
input_source = "cia"        # cia | kernal | auto | none
min_duration_s = 0
reset_before_launch = true
```

<!-- table: fields -->
| Key | Description |
|---|---|
| **`file`**<br>*Type:* `str \| None`<br>*Default:* `None` | Asset spec (comma-separated paths/dirs/globs). Videos for video, .sid for waveform, images for slideshow, .prg/.crt for launcher, .sid for generative when audio_source = sid. |
| **`input_source`**<br>*Type:* `str`<br>*Default:* `'cia'` | What counts as player input to reset the idle timeout: 'cia' (joystick bits at $DC00/$DC01), 'kernal' ($00C5/$00C6, only live while the kernal IRQ runs), 'auto' (both), or 'none' (pure timer, for demos). Never counts C=/SHIFT/CTRL. Choices: `cia`, `kernal`, `auto`, `none`. |
| **`max_duration_s`**<br>*Type:* `float \| None`<br>*Default:* `None` | Hard ceiling in seconds — advance regardless of input. Unset = no cap (a continuously-played game runs forever). |
| **`min_duration_s`**<br>*Type:* `float`<br>*Default:* `0.0` | Floor in seconds before the idle timeout can advance the scene, even if no input is seen. |
| **`reset_before_launch`**<br>*Type:* `bool`<br>*Default:* `True` | Reset the U64 before launching for a clean machine state. |
| **`bypass_audio_lock`**<br>*Type:* `bool`<br>*Default:* `False` | Ensemble: don't contend for the exclusive audio slot — the launched program drives its own SID concurrently, so several people can play (and hear) their own games at once. No effect single-system. |

## `midi`

Live MIDI input → SID synth + 3-voice oscilloscope (bitmap-only).

```toml
[[scenes]]
type = "midi"
color_mode = "per_voice"  # per_voice | per_waveform
time_base = "wallclock"   # wallclock | auto
auto_cycles = 4
```

<!-- table: fields -->
| Key | Description |
|---|---|
| **`color_mode`**<br>*Type:* `str`<br>*Default:* `'per_voice'` | Oscilloscope coloring: fixed per voice, or by current waveform type. Choices: `per_voice`, `per_waveform`. |
| **`voice_colors`**<br>*Type:* `list[str]`<br>*Default:* `[]` | Per-voice trace colors (C64 color names) for color_mode=per_voice. |
| **`waveform_colors`**<br>*Type:* `dict[str, str]`<br>*Default:* `{}` | Per-waveform-type colors (e.g. pulse=cyan) for color_mode=per_waveform. |
| **`time_base`**<br>*Type:* `str`<br>*Default:* `'wallclock'` | Scope time window: 'wallclock' (1 row = 1 frame) or 'auto' (per-voice window sized so auto_cycles cycles fit). Choices: `wallclock`, `auto`. |
| **`auto_cycles`**<br>*Type:* `float`<br>*Default:* `4.0` | Complete cycles per render window when time_base = 'auto'. |
| **`persistence`**<br>*Type:* `str`<br>*Default:* `'off'` | Trace decay/trail length ('off' redraws each frame). Choices: `off`, `short`, `medium`, `long`, `random`. |
| **`scroll_columns`**<br>*Type:* `int \| list[int]`<br>*Default:* `0` | FIFO-scroll the strip left by N columns/frame (0 = redraw). Int or a list of 3 per-voice ints. |
| **`midi_port`**<br>*Type:* `str \| None`<br>*Default:* `None` | MIDI input port name substring; unset = first available port. |
| **`midi_waveform`**<br>*Type:* `str`<br>*Default:* `'pulse'` | Default SID waveform for MIDI notes (the starting waveform for every voice; SHIFT cycles it, incl. into combined waveforms). Choices: `triangle`, `sawtooth`, `pulse`, `noise`. |
| **`midi_voice_waveforms`**<br>*Type:* `list[str]`<br>*Default:* `[]` | Per-voice starting waveforms (up to 3, e.g. ['pulse', 'sawtooth', 'triangle']). Each entry is one waveform or a '+'-combo. 'pulse+triangle' is the combined wave that reliably sounds on a 6581; sawtooth combos AND down to near-silence there (audible may differ on 8580). Empty = every voice uses midi_waveform; fewer than 3 repeats the last. |
| **`midi_voice_mode`**<br>*Type:* `str`<br>*Default:* `'shared'` | Voice allocation: 'shared' = one MIDI channel spread across the 3 voices (mono melody over a sustain pad); 'multitimbral' = MIDI channels route to fixed voices (see midi_voice_channels). Choices: `shared`, `multitimbral`. |
| **`midi_voice_channels`**<br>*Type:* `list[int]`<br>*Default:* `[1, 2, 3]` | Multitimbral channel→voice map: MIDI channels (1..16) for voices 1/2/3, in order. Only used when midi_voice_mode = 'multitimbral'; notes on other channels are ignored. |
| **`midi_program_change`**<br>*Type:* `bool`<br>*Default:* `True` | Honor MIDI Program Change to select a voice's waveform (shared mode = all voices; multitimbral = the message's channel). |
| **`midi_adsr`**<br>*Type:* `list[int]`<br>*Default:* `[0, 8, 12, 8]` | ADSR envelope as [attack, decay, sustain, release] (4 nibbles 0..15). |
| **`midi_pulse_width`**<br>*Type:* `int`<br>*Default:* `2048` | SID pulse width (0..4095) when midi_waveform = 'pulse'. Swept live by CC1 (mod wheel). |
| **`midi_filter_cutoff`**<br>*Type:* `int`<br>*Default:* `2047` | SID filter cutoff (0..2047); all voices are routed through the filter. Default open (neutral lowpass); swept live by CC74. |
| **`midi_filter_resonance`**<br>*Type:* `int`<br>*Default:* `0` | SID filter resonance (0..15) for MIDI notes; swept live by CC71. |
| **`midi_filter_mode`**<br>*Type:* `str`<br>*Default:* `'lowpass'` | SID filter mode for MIDI notes. Choices: `lowpass`, `bandpass`, `highpass`. |
| **`midi_master_volume`**<br>*Type:* `int`<br>*Default:* `15` | SID master volume nibble (0..15) for MIDI notes; CC7. |

## `slideshow`

Cycle through still images, each stylized through a display mode.

Display modes: `mhires`, `hires`, `hires_edges`, `mcm`, `petscii`, `random`.

```toml
[[scenes]]
type = "slideshow"
image_duration_s = 5
aspect_mode = "crop"  # crop | fit | stretch
mod_source = "audio"  # audio | clock | off
```

<!-- table: fields -->
| Key | Description |
|---|---|
| **`display`**<br>*Type:* `str \| None`<br>*Default:* `None` | VIC-II display mode. Unset resolves per scene type: 'mhires' for video (richest bitmap mode, suits arbitrary film/photo content) and 'hires_edges' for webcam/blank/slideshow/generative (tuned for live Canny-edge stylization). waveform and midi are bitmap-only (both ignore this); slideshow also accepts 'random'. generative renders a frame so any quantizing mode works (not 'blank'/'random'). Choices: `hires_edges`, `hires`, `petscii`, `mcm`, `mhires`, `blank`, `random`. |
| **`file`**<br>*Type:* `str \| None`<br>*Default:* `None` | Asset spec (comma-separated paths/dirs/globs). Videos for video, .sid for waveform, images for slideshow, .prg/.crt for launcher, .sid for generative when audio_source = sid. |
| **`image_duration_s`**<br>*Type:* `float`<br>*Default:* `5.0` | Per-image dwell time before advancing (total runtime is duration_s). |
| **`aspect_mode`**<br>*Type:* `str`<br>*Default:* `'crop'` | How each image is fit to the C64 4:2.5 aspect: 'crop' (center-crop to fill — the default, edges lost), 'fit' (letterbox/pillarbox so the whole image shows, padded black), or 'stretch' (distort to fill, no padding or cropping). Choices: `crop`, `fit`, `stretch`. |
| **`effect`**<br>*Type:* `str \| None`<br>*Default:* `None` | Pixel effect applied to the frame before quantization (unset = none). Works on any frame-bearing scene. 'trails' echoes moving content; 'pulse' beat-punches the zoom; 'rgb_shift' slews the color channels apart on a transient. pulse/rgb_shift only visibly react on a music-reactive scene (generative + audio_source = 'sid'); elsewhere they're inert (no feature stream to react to). Choices: `trails`, `pulse`, `rgb_shift`, `blur`, `strobe`, `invert`, `mirror`, `posterize`. |
| **`effects`**<br>*Type:* `list[str]`<br>*Default:* `[]` | Ordered pixel-effect chain applied before quantization, e.g. effects = ["trails", "rgb_shift", "strobe"]. Each is one of the `effect` choices; layers apply in order and are individually tunable (map a CC to fx0.<param>/fx1.<param>…) and bypass-toggleable live (fx_toggle). Mutually exclusive with the single `effect` field. Empty = none. Choices: `trails`, `pulse`, `rgb_shift`, `blur`, `strobe`, `invert`, `mirror`, `posterize`. |
| **`mod_source`**<br>*Type:* `str`<br>*Default:* `'audio'` | What drives this scene's reactive effect layers: 'audio' (the SID feature stream — needs a music-reactive scene, i.e. generative + audio_source = 'sid'), 'clock' (the [performance] beat grid, so effects lock to MIDI/tap tempo on any scene — the way to tempo-lock a 'strobe'), or 'off' (never react — layers use their static baseline). Applies to every effect layer on the scene. Choices: `audio`, `clock`, `off`. |
| **`palette_mode`**<br>*Type:* `str`<br>*Default:* `'percell'` | VIC-II slot-allocation strategy for mcm/mhires display (ignored by other modes): percell (default), cheap, vivid, grayscale. Color shaping (channel boost + hue corrections, e.g. the purple rescue) is the global [color] section, applied to every mode. Choices: `percell`, `cheap`, `vivid`, `grayscale`. *Live-tunable* while a show runs, as `mode.palette_mode` — Appendix F. *Menu-live*: the on-C64 menu offers this knob, applied to the running scene. |
| **`text_double_height`**<br>*Type:* `bool`<br>*Default:* `False` | On mhires, render text overlays (clock/marquee/…) at double height — 16px / 2 cell rows — for across-the-room legibility. Text is always double-WIDE on mhires (8x8 glyph spans 2 of the 4px cells); this toggle adds the vertical stretch. Ignored on other display modes. |
| **`style`**<br>*Type:* `str`<br>*Default:* `'default'` | PETSCII glyph/color style (only when display = 'petscii'); 'random' picks one at setup. Choices: `default`, `halftone`, `random_glyph`, `letter_rain`, `neon`, `inverse_pop`, `hatch`, `color_only`, `random`. *Menu-live*: the on-C64 menu offers this knob, applied to the running scene. |
| **`color`**<br>*Type:* `dict[str, Any]`<br>*Default:* `{}` | Per-scene [color] override ([scenes.color] sub-table): any [color] field set here replaces the global value for this scene only. Unset fields follow the global [color] section. See `--describe color` for the field list. |

## `video`

Play a video file with synced audio until it ends.

Display modes: `mhires`, `hires_edges`, `hires`, `mcm`, `petscii`, `blank`.

```toml
[[scenes]]
type = "video"
mod_source = "audio"        # audio | clock | off
palette_mode = "percell"
text_double_height = false
```

<!-- table: fields -->
| Key | Description |
|---|---|
| **`display`**<br>*Type:* `str \| None`<br>*Default:* `None` | VIC-II display mode. Unset resolves per scene type: 'mhires' for video (richest bitmap mode, suits arbitrary film/photo content) and 'hires_edges' for webcam/blank/slideshow/generative (tuned for live Canny-edge stylization). waveform and midi are bitmap-only (both ignore this); slideshow also accepts 'random'. generative renders a frame so any quantizing mode works (not 'blank'/'random'). Choices: `hires_edges`, `hires`, `petscii`, `mcm`, `mhires`, `blank`, `random`. |
| **`file`**<br>*Type:* `str \| None`<br>*Default:* `None` | Asset spec (comma-separated paths/dirs/globs). Videos for video, .sid for waveform, images for slideshow, .prg/.crt for launcher, .sid for generative when audio_source = sid. |
| **`start_s`**<br>*Type:* `float \| None`<br>*Default:* `None` | Seconds into the source to begin playback (video only). Quick playback fills this from a URL's t=/start= timestamp; can also be set directly on a [[scenes]] video. Unset/0 = play from the start. |
| **`audio`**<br>*Type:* `bool \| None`<br>*Default:* `None` | Per-scene audio override. Unset follows [audio].enabled; false mutes this scene only. |
| **`effect`**<br>*Type:* `str \| None`<br>*Default:* `None` | Pixel effect applied to the frame before quantization (unset = none). Works on any frame-bearing scene. 'trails' echoes moving content; 'pulse' beat-punches the zoom; 'rgb_shift' slews the color channels apart on a transient. pulse/rgb_shift only visibly react on a music-reactive scene (generative + audio_source = 'sid'); elsewhere they're inert (no feature stream to react to). Choices: `trails`, `pulse`, `rgb_shift`, `blur`, `strobe`, `invert`, `mirror`, `posterize`. |
| **`effects`**<br>*Type:* `list[str]`<br>*Default:* `[]` | Ordered pixel-effect chain applied before quantization, e.g. effects = ["trails", "rgb_shift", "strobe"]. Each is one of the `effect` choices; layers apply in order and are individually tunable (map a CC to fx0.<param>/fx1.<param>…) and bypass-toggleable live (fx_toggle). Mutually exclusive with the single `effect` field. Empty = none. Choices: `trails`, `pulse`, `rgb_shift`, `blur`, `strobe`, `invert`, `mirror`, `posterize`. |
| **`mod_source`**<br>*Type:* `str`<br>*Default:* `'audio'` | What drives this scene's reactive effect layers: 'audio' (the SID feature stream — needs a music-reactive scene, i.e. generative + audio_source = 'sid'), 'clock' (the [performance] beat grid, so effects lock to MIDI/tap tempo on any scene — the way to tempo-lock a 'strobe'), or 'off' (never react — layers use their static baseline). Applies to every effect layer on the scene. Choices: `audio`, `clock`, `off`. |
| **`pre_emphasis`**<br>*Type:* `float \| None`<br>*Default:* `None` | Per-scene HF pre-emphasis (0 = off, ~0.3-0.7 typical; brightens speech). Unset = global [dsp].pre_emphasis / source-aware default. Needs [dsp].enabled + scene audio. |
| **`palette_mode`**<br>*Type:* `str`<br>*Default:* `'percell'` | VIC-II slot-allocation strategy for mcm/mhires display (ignored by other modes): percell (default), cheap, vivid, grayscale. Color shaping (channel boost + hue corrections, e.g. the purple rescue) is the global [color] section, applied to every mode. Choices: `percell`, `cheap`, `vivid`, `grayscale`. *Live-tunable* while a show runs, as `mode.palette_mode` — Appendix F. *Menu-live*: the on-C64 menu offers this knob, applied to the running scene. |
| **`text_double_height`**<br>*Type:* `bool`<br>*Default:* `False` | On mhires, render text overlays (clock/marquee/…) at double height — 16px / 2 cell rows — for across-the-room legibility. Text is always double-WIDE on mhires (8x8 glyph spans 2 of the 4px cells); this toggle adds the vertical stretch. Ignored on other display modes. |
| **`style`**<br>*Type:* `str`<br>*Default:* `'default'` | PETSCII glyph/color style (only when display = 'petscii'); 'random' picks one at setup. Choices: `default`, `halftone`, `random_glyph`, `letter_rain`, `neon`, `inverse_pop`, `hatch`, `color_only`, `random`. *Menu-live*: the on-C64 menu offers this knob, applied to the running scene. |
| **`color`**<br>*Type:* `dict[str, Any]`<br>*Default:* `{}` | Per-scene [color] override ([scenes.color] sub-table): any [color] field set here replaces the global value for this scene only. Unset fields follow the global [color] section. See `--describe color` for the field list. |

## `waveform`

3-voice SID oscilloscope playing a .sid file (bitmap-only).

```toml
[[scenes]]
type = "waveform"
song = 0
color_mode = "per_voice"  # per_voice | per_waveform
time_base = "wallclock"   # wallclock | auto
```

<!-- table: fields -->
| Key | Description |
|---|---|
| **`file`**<br>*Type:* `str \| None`<br>*Default:* `None` | Asset spec (comma-separated paths/dirs/globs). Videos for video, .sid for waveform, images for slideshow, .prg/.crt for launcher, .sid for generative when audio_source = sid. |
| **`song`**<br>*Type:* `int`<br>*Default:* `0` | SID subtune index to play (0 = the SID's default; 1-based otherwise). For generative scenes, only with audio_source = sid. |
| **`color_mode`**<br>*Type:* `str`<br>*Default:* `'per_voice'` | Oscilloscope coloring: fixed per voice, or by current waveform type. Choices: `per_voice`, `per_waveform`. |
| **`voice_colors`**<br>*Type:* `list[str]`<br>*Default:* `[]` | Per-voice trace colors (C64 color names) for color_mode=per_voice. |
| **`waveform_colors`**<br>*Type:* `dict[str, str]`<br>*Default:* `{}` | Per-waveform-type colors (e.g. pulse=cyan) for color_mode=per_waveform. |
| **`time_base`**<br>*Type:* `str`<br>*Default:* `'wallclock'` | Scope time window: 'wallclock' (1 row = 1 frame) or 'auto' (per-voice window sized so auto_cycles cycles fit). Choices: `wallclock`, `auto`. |
| **`auto_cycles`**<br>*Type:* `float`<br>*Default:* `4.0` | Complete cycles per render window when time_base = 'auto'. |
| **`persistence`**<br>*Type:* `str`<br>*Default:* `'off'` | Trace decay/trail length ('off' redraws each frame). Choices: `off`, `short`, `medium`, `long`, `random`. |
| **`scroll_columns`**<br>*Type:* `int \| list[int]`<br>*Default:* `0` | FIFO-scroll the strip left by N columns/frame (0 = redraw). Int or a list of 3 per-voice ints. |

## `webcam`

Live webcam feed stylized through a display mode.

Display modes: `hires_edges`, `hires`, `mhires`, `mcm`, `petscii`, `blank`.

```toml
[[scenes]]
type = "webcam"
mod_source = "audio"        # audio | clock | off
palette_mode = "percell"
text_double_height = false
```

<!-- table: fields -->
| Key | Description |
|---|---|
| **`display`**<br>*Type:* `str \| None`<br>*Default:* `None` | VIC-II display mode. Unset resolves per scene type: 'mhires' for video (richest bitmap mode, suits arbitrary film/photo content) and 'hires_edges' for webcam/blank/slideshow/generative (tuned for live Canny-edge stylization). waveform and midi are bitmap-only (both ignore this); slideshow also accepts 'random'. generative renders a frame so any quantizing mode works (not 'blank'/'random'). Choices: `hires_edges`, `hires`, `petscii`, `mcm`, `mhires`, `blank`, `random`. |
| **`audio`**<br>*Type:* `bool \| None`<br>*Default:* `None` | Per-scene audio override. Unset follows [audio].enabled; false mutes this scene only. |
| **`effect`**<br>*Type:* `str \| None`<br>*Default:* `None` | Pixel effect applied to the frame before quantization (unset = none). Works on any frame-bearing scene. 'trails' echoes moving content; 'pulse' beat-punches the zoom; 'rgb_shift' slews the color channels apart on a transient. pulse/rgb_shift only visibly react on a music-reactive scene (generative + audio_source = 'sid'); elsewhere they're inert (no feature stream to react to). Choices: `trails`, `pulse`, `rgb_shift`, `blur`, `strobe`, `invert`, `mirror`, `posterize`. |
| **`effects`**<br>*Type:* `list[str]`<br>*Default:* `[]` | Ordered pixel-effect chain applied before quantization, e.g. effects = ["trails", "rgb_shift", "strobe"]. Each is one of the `effect` choices; layers apply in order and are individually tunable (map a CC to fx0.<param>/fx1.<param>…) and bypass-toggleable live (fx_toggle). Mutually exclusive with the single `effect` field. Empty = none. Choices: `trails`, `pulse`, `rgb_shift`, `blur`, `strobe`, `invert`, `mirror`, `posterize`. |
| **`mod_source`**<br>*Type:* `str`<br>*Default:* `'audio'` | What drives this scene's reactive effect layers: 'audio' (the SID feature stream — needs a music-reactive scene, i.e. generative + audio_source = 'sid'), 'clock' (the [performance] beat grid, so effects lock to MIDI/tap tempo on any scene — the way to tempo-lock a 'strobe'), or 'off' (never react — layers use their static baseline). Applies to every effect layer on the scene. Choices: `audio`, `clock`, `off`. |
| **`pre_emphasis`**<br>*Type:* `float \| None`<br>*Default:* `None` | Per-scene HF pre-emphasis (0 = off, ~0.3-0.7 typical; brightens speech). Unset = global [dsp].pre_emphasis / source-aware default. Needs [dsp].enabled + scene audio. |
| **`palette_mode`**<br>*Type:* `str`<br>*Default:* `'percell'` | VIC-II slot-allocation strategy for mcm/mhires display (ignored by other modes): percell (default), cheap, vivid, grayscale. Color shaping (channel boost + hue corrections, e.g. the purple rescue) is the global [color] section, applied to every mode. Choices: `percell`, `cheap`, `vivid`, `grayscale`. *Live-tunable* while a show runs, as `mode.palette_mode` — Appendix F. *Menu-live*: the on-C64 menu offers this knob, applied to the running scene. |
| **`text_double_height`**<br>*Type:* `bool`<br>*Default:* `False` | On mhires, render text overlays (clock/marquee/…) at double height — 16px / 2 cell rows — for across-the-room legibility. Text is always double-WIDE on mhires (8x8 glyph spans 2 of the 4px cells); this toggle adds the vertical stretch. Ignored on other display modes. |
| **`style`**<br>*Type:* `str`<br>*Default:* `'default'` | PETSCII glyph/color style (only when display = 'petscii'); 'random' picks one at setup. Choices: `default`, `halftone`, `random_glyph`, `letter_rain`, `neon`, `inverse_pop`, `hatch`, `color_only`, `random`. *Menu-live*: the on-C64 menu offers this knob, applied to the running scene. |
| **`color`**<br>*Type:* `dict[str, Any]`<br>*Default:* `{}` | Per-scene [color] override ([scenes.color] sub-table): any [color] field set here replaces the global value for this scene only. Unset fields follow the global [color] section. See `--describe color` for the field list. |

## `wled`

Virtual WLED LED matrix — receive a realtime pixel stream (DDP / WLED UDP from LedFx/xLights) and render it to any display mode.

Display modes: `mhires`, `hires`, `hires_edges`, `mcm`, `petscii`.

```toml
[[scenes]]
type = "wled"
sink_width = 320
sink_height = 200
mod_source = "audio"  # audio | clock | off
```

<!-- table: fields -->
| Key | Description |
|---|---|
| **`display`**<br>*Type:* `str \| None`<br>*Default:* `None` | VIC-II display mode. Unset resolves per scene type: 'mhires' for video (richest bitmap mode, suits arbitrary film/photo content) and 'hires_edges' for webcam/blank/slideshow/generative (tuned for live Canny-edge stylization). waveform and midi are bitmap-only (both ignore this); slideshow also accepts 'random'. generative renders a frame so any quantizing mode works (not 'blank'/'random'). Choices: `hires_edges`, `hires`, `petscii`, `mcm`, `mhires`, `blank`, `random`. |
| **`sink_width`**<br>*Type:* `int`<br>*Default:* `320` | WLED sink: virtual LED-matrix width in pixels a sender streams to (wled scenes only). Must match the sender's configured matrix; the display mode downscales it to the C64. Default 320. |
| **`sink_height`**<br>*Type:* `int`<br>*Default:* `200` | WLED sink: virtual LED-matrix height in pixels a sender streams to (wled scenes only). Must match the sender's configured matrix; the display mode downscales it to the C64. Default 200. |
| **`effect`**<br>*Type:* `str \| None`<br>*Default:* `None` | Pixel effect applied to the frame before quantization (unset = none). Works on any frame-bearing scene. 'trails' echoes moving content; 'pulse' beat-punches the zoom; 'rgb_shift' slews the color channels apart on a transient. pulse/rgb_shift only visibly react on a music-reactive scene (generative + audio_source = 'sid'); elsewhere they're inert (no feature stream to react to). Choices: `trails`, `pulse`, `rgb_shift`, `blur`, `strobe`, `invert`, `mirror`, `posterize`. |
| **`effects`**<br>*Type:* `list[str]`<br>*Default:* `[]` | Ordered pixel-effect chain applied before quantization, e.g. effects = ["trails", "rgb_shift", "strobe"]. Each is one of the `effect` choices; layers apply in order and are individually tunable (map a CC to fx0.<param>/fx1.<param>…) and bypass-toggleable live (fx_toggle). Mutually exclusive with the single `effect` field. Empty = none. Choices: `trails`, `pulse`, `rgb_shift`, `blur`, `strobe`, `invert`, `mirror`, `posterize`. |
| **`mod_source`**<br>*Type:* `str`<br>*Default:* `'audio'` | What drives this scene's reactive effect layers: 'audio' (the SID feature stream — needs a music-reactive scene, i.e. generative + audio_source = 'sid'), 'clock' (the [performance] beat grid, so effects lock to MIDI/tap tempo on any scene — the way to tempo-lock a 'strobe'), or 'off' (never react — layers use their static baseline). Applies to every effect layer on the scene. Choices: `audio`, `clock`, `off`. |
| **`palette_mode`**<br>*Type:* `str`<br>*Default:* `'percell'` | VIC-II slot-allocation strategy for mcm/mhires display (ignored by other modes): percell (default), cheap, vivid, grayscale. Color shaping (channel boost + hue corrections, e.g. the purple rescue) is the global [color] section, applied to every mode. Choices: `percell`, `cheap`, `vivid`, `grayscale`. *Live-tunable* while a show runs, as `mode.palette_mode` — Appendix F. *Menu-live*: the on-C64 menu offers this knob, applied to the running scene. |
| **`text_double_height`**<br>*Type:* `bool`<br>*Default:* `False` | On mhires, render text overlays (clock/marquee/…) at double height — 16px / 2 cell rows — for across-the-room legibility. Text is always double-WIDE on mhires (8x8 glyph spans 2 of the 4px cells); this toggle adds the vertical stretch. Ignored on other display modes. |
| **`style`**<br>*Type:* `str`<br>*Default:* `'default'` | PETSCII glyph/color style (only when display = 'petscii'); 'random' picks one at setup. Choices: `default`, `halftone`, `random_glyph`, `letter_rain`, `neon`, `inverse_pop`, `hatch`, `color_only`, `random`. *Menu-live*: the on-C64 menu offers this knob, applied to the running scene. |
| **`color`**<br>*Type:* `dict[str, Any]`<br>*Default:* `{}` | Per-scene [color] override ([scenes.color] sub-table): any [color] field set here replaces the global value for this scene only. Unset fields follow the global [color] section. See `--describe color` for the field list. |
