---
number: 2
---

# Scenes and Overlays

Everything c64cast puts on a screen is a scene with overlays painted over it.
There are ten kinds of scene and thirteen overlays. This chapter is the
catalogue: what each one sources, what it requires, what it does that its
parameter table cannot say, and a configuration for each that runs as written.

The parameter tables themselves are Appendices B and C, and the question of
which overlay a given display mode will accept is Appendix D. The same
material is at the terminal as `c64cast --list-scenes`, `--list-overlays` and
`--describe scene:NAME`.

## The Shape of a Scene

A scene is a `[[scenes]]` table with a `type`. Whatever the type, the playlist
treats it the same way: it is set up, its frames are processed one at a time
until it declares itself done, and it is torn down. In a multi-scene playlist
an interstitial card runs between one scene and the next.

Six keys are common to every type — `type`, `name`, `duration_s`,
`target_fps`, `overlays`, and the ensemble pair `orchestrate` and
`follower_only`. Most types add `display`, and the frame-bearing ones add the
colour and effect keys covered in Chapter 3.

### What Ends a Scene

Four things, in this order of authority:

**A CTRL skip** always wins, immediately, from the keyboard, the control
plane, a MIDI pad or a gesture.

**The source running out.** A video scene ends when its file does; this is why
`duration_s` is rejected on a video scene rather than accepted and ignored.

**`duration_s` elapsing.** The default depends on the type: a waveform scene
without one plays for the tune's real length if the song-length database is
loaded, and 30 seconds otherwise; slideshow and generative scenes default to
30 seconds; webcam and blank scenes run forever *when they are the only scene*
and 30 seconds otherwise, because an infinite scene in a rotation would never
hand over. An explicit `duration_s = 0` means "run forever" for every type
except video. A negative value is rejected.

**An overlay reporting itself busy** defers the handover: when the timer
expires the playlist asks every attached overlay whether it is mid-something,
and gives the scene another frame if any says yes. A `big_text` overlay with
`loop = false` uses this to finish scrolling its last message before the
interstitial appears.

### Frame Rate

`target_fps` caps a scene's rate. Unset, it is the system rate — 60 on NTSC,
50 on PAL — except where the DMA link cannot carry that much traffic:

| Scene | Default cap |
|---|---|
| Bitmap `video` / `webcam` / `generative`, streaming digitised audio | 20 fps |
| The same, muted | half system rate (30 / 25) |
| `generative` and live `webcam` with audio on the 4-bit DAC | 20 fps *in any mode*, character modes included |
| `waveform`, `midi`, `asid` | half system rate (30 / 25) |
| Everything else | system rate |

The exception worth knowing is video on the Ultimate Audio sampler: that audio
is off the Commodore's bus entirely, so the scene keeps the full system rate.
An explicit `target_fps` always wins over every row of that table.

### Audio

A scene that can make sound follows `[audio].enabled`, and a per-scene
`audio = false` mutes that one scene without touching the rest of the
playlist. In an ensemble, only one system may hold the audio slot at a time;
a system whose playlist is *entirely* audio-bearing scenes will idle whenever
another system holds it, and the loader warns about that at load.

### Files

Scenes that read media take the asset spec described in Chapter 1: a
comma-separated list of paths, directories and globs, re-resolved at every
setup with one member picked at random. Each type has a default directory it
falls back to when `file` is omitted, named in that type's entry below.

## Between One Scene and the Next

A multi-scene playlist does not cut from one scene to another. It shows a card
first — the words UP NEXT over the upcoming scene's `name`, centred on an
animated parallax background — for as long as `[interstitial]` says.

```toml
[interstitial]
duration_s = 4.0
text_color = "rainbow"
background = "starfield"
```

`text_color` takes a colour, or `"rainbow"` for a colour per line, or
`"random"` for one legible colour drawn fresh at each card. `background` is
one of `starfield`, `petscii_bars`, `raster_bars`, `checker`, `nature`,
`city`, `none`, or `random` for a different one each time; it scrolls in the
rows above and below the text block and never paints over the words.

The card is a PETSCII scene of its own whatever the scenes on either side of
it are, so it costs the same between two bitmap scenes as between two
character ones. It appears before the first scene as well as between scenes.

The name it shows is the real one. A scene whose `file` is a directory makes
its pick *before* the card is built, so a jukebox announces the tune that is
about to play rather than the folder it came from.

Three things bypass it. Single-scene mode never builds one. A jump — a MIDI
pad, a clip launch, a control-plane request — lands on its scene directly,
because a cue that then waits four seconds is not a cue. And a CTRL skip
during the card ends the card and starts the scene it was announcing.

Two other things happen at a scene boundary. `[playlist].fade_duration_s` is
the fade to and from black at each end of a scene, on every mode that
composes a frame; a CTRL skip abandons an unfinished fade rather than waiting
for it. And `[playlist].interleave_videos` inserts a video from
`[playlist].videos_dir` after every scene that is not itself a video, taking
them in turn, each one rendered in `hires_edges`. It needs the `video` extra
and a multi-scene playlist: in single-scene mode it would quietly make the
playlist two scenes long and defeat the mode, so it is skipped with a log
line.

## The Scene Types

In alphabetical order. Every configuration here is complete: paste it into a
file and it runs.

Each type opens with the same three facts under its configuration: what it
needs installed, where it looks for files when `file` is omitted, and which
display modes it accepts. Appendix B is the full key-by-key table for all ten,
and `c64cast --describe scene:NAME` prints any one of them at the terminal.

### `asid`

Receives an ASID stream — SID register writes packed into MIDI SysEx — and
plays it on the real chip, with the three-voice oscilloscope drawn alongside.

```toml
[[scenes]]
type = "asid"
name = "Incoming"
duration_s = 300.0
color_mode = "per_voice"
```

*Needs the `midi` extra. Takes no files — the stream is the source.
Bitmap-only: `display` is ignored.*

ASID rides the MIDI transport, which is where the extra comes from. Point an
ASID host at a port c64cast can open: DeepSID in a browser, SIDFactory II,
Plogue chipsynth C64, an Elektron with ASID-XP. On macOS enable the IAC driver
in Audio MIDI Setup; on Linux `modprobe snd-virmidi`.

This scene has no synthesiser knobs, because ASID carries the whole tune's
register state and c64cast is only relaying it. What it has instead is three
keys about how that state is delivered. `asid_multi_sid` and `asid_max_sids`
gate and cap the routing of a multi-SID stream onto extra chips, which needs
a machine whose SID addresses c64cast can configure. `asid_buffered_player`
chooses between the two ways of playing what arrives, and its default `"auto"`
takes the accurate one wherever the machine can carry it. Chapter 4 has what
those two ways are, why one of them needs expansion memory, and what the
protocol does and does not carry.

### `blank`

A solid canvas with no video input at all: every cell is a space in the
background colour, until an overlay paints.

```toml
[[scenes]]
type = "blank"
name = "Title Card"
duration_s = 12.0
border = "black"
background = "blue"
```

*Needs no extra. Takes no files. Always the `blank` display mode, whatever
`display` says.*

Its purpose is to be a foundation. Every overlay that works on a character
mode works here, so blank plus `big_text` is a demo-scene title card, and
blank plus a clock and the weather is an information board. As the only scene
in a playlist it runs forever.

### `generative`

Procedural pictures computed on the host, optionally reacting to music. Three
orthogonal choices: a frame `source`, an `audio_source`, and an effect chain.

```toml
[[scenes]]
type = "generative"
name = "Plasma"
display = "mhires"
source = "plasma"
audio_source = "sid"
file = "~/Music/hvsc/MUSICIANS/H/Hubbard_Rob"
reactive = true
effects = ["trails"]
duration_s = 90.0
```

*Needs the `mic` extra for `audio_source = "mic"` or `"listen"` and the `video`
extra for `"file"`; no extra otherwise. `file` defaults to `assets/sids/` for a
`sid` source and is required for a `file` one. Display modes: `hires_edges`
(the default), `hires`, `mhires`, `mcm`, `petscii`.*

The twenty sources and eight effects are Appendix E; the pipeline they feed is
Chapter 3. `audio_source` is what turns a pattern into a visualiser:

| `audio_source` | What it does |
|---|---|
| `none` | Silent, and the pattern runs on its own clock. The default |
| `sid` | Play the `file` tune on the real chip and react to it |
| `file` | Decode the `file` audio track to the DAC and react to it |
| `mic` | Stream live input to the DAC and react to it |
| `listen` | React to live input, and send no audio to the Commodore |

`listen` is the VJ arrangement: the real sound is on a PA, and only the
picture tracks it. Freed from the DAC's rate it also captures at full
bandwidth, so its transients are cleaner than `mic`'s.

`reactive = false` keeps the pure time-driven look with the audio still
playing. A `sid` source forces the host-DMA display path and pairs most
reliably with a character mode (`petscii` or `mcm`); a bitmap mode works only
with a tune that loads high enough to clear `$2000`.

### `launcher`

Hands the machine over to a native Commodore program, then takes it back.

```toml
[[scenes]]
type = "launcher"
name = "Games"
file = "~/c64/programs"
duration_s = 120.0
input_source = "cia"
min_duration_s = 30.0
reset_before_launch = true
```

*Needs no extra. `file` defaults to `assets/programs/`. No display mode: the
program owns the screen.*

The machine is reset for a clean state, then the file is uploaded and run — a
`.prg` loaded and run, a `.crt` started as a cartridge, chosen by extension.
From that moment the program owns the VIC, the SID and the CIAs; c64cast stops
painting and only watches for input.

> [!WARNING]
> This scene hands the machine away, and the reset before launch discards
> whatever was in memory. The modifier keys are read out of the kernal's
> scratch bytes, which a program that installs its own interrupt stops
> updating — so pause and skip may not answer for as long as the program
> holds the machine. `max_duration_s` is the ceiling that always fires.

`duration_s` here is an **idle timeout**, not a runtime. It counts down from
launch and resets on every sign of a player, so a game in use stays up while
an untouched demo advances. `min_duration_s` is a floor before the idle timer
may fire at all, and `max_duration_s` an optional hard ceiling.

`input_source` decides what counts as a player: `cia` reads the joystick bits
at `$DC00`/`$DC01` and works whatever the program did with the interrupt;
`kernal` reads the kernal's keyboard scratch, which is clean but only alive
while the kernal interrupt runs; `auto` takes either; `none` polls nothing and
leaves `duration_s` a plain timer, which is what a demo wants. The pause and
skip modifier keys are deliberately never counted as play.

In an ensemble, `bypass_audio_lock = true` lets several launcher systems run
at once, each player hearing their own machine.

### `midi`

Turns the Commodore into a three-voice synthesiser played from a MIDI
keyboard, with the same oscilloscope as the waveform scene.

```toml
[[scenes]]
type = "midi"
name = "SID Synth"
duration_s = 300.0
midi_voice_waveforms = ["pulse", "sawtooth", "triangle"]
midi_adsr = [0, 8, 12, 8]
midi_filter_cutoff = 1024
```

*Needs the `midi` extra. Takes no files — the keyboard is the source.
Bitmap-only: `display` is ignored.*

Each voice can hold its own waveform, and an entry may be a `+`-combination
for the chip's combined waveforms. `midi_voice_mode` picks between the default
`shared`, where one channel spreads across all three voices, and
`multitimbral`, where `midi_voice_channels` pins a channel to each. Chapter 4
has how voices are allocated and stolen under each mode, what pitch-bend and
velocity reach, and the controller map.

> [!NOTE]
> On a 6581 the waveform outputs share a bus and combine by AND, and any
> combination containing sawtooth ANDs down to near-silence. `pulse+triangle`
> is the one combination that reliably sounds, which is why it is the only one
> in the interactive rotation. The others remain settable for experiment, and
> may behave differently on an 8580.

### `slideshow`

Still images, fitted to the screen and quantised into the palette.

```toml
[[scenes]]
type = "slideshow"
name = "Photographs"
display = "mhires"
file = "~/Pictures/*.jpg"
duration_s = 120.0
image_duration_s = 5.0
aspect_mode = "crop"
```

*Needs no extra. `file` defaults to `assets/pictures/`. Display modes:
`mhires` (the default, and what `hires_edges` becomes here), `hires`, `mcm`,
`petscii`, and `random`.*

Two durations doing different jobs: `image_duration_s` is how long one picture
holds, `duration_s` how long the whole scene runs. The picker shuffles and
walks the pool, so every image appears once before any repeats and none
appears twice in a row, including across a reshuffle. Anything OpenCV decodes
is accepted.

`aspect_mode` reconciles the image with the Commodore's 4:2.5 pixel geometry:
`crop` centre-crops to fill and loses the edges, `fit` letterboxes the whole
image onto black, `stretch` distorts to fill.

Two display notes particular to this type: `display = "random"` picks a fresh
mode at every setup, so a single-scene slideshow varies as it loops, and
`display = "hires_edges"` is substituted with `mhires` — edge detection is
tuned for a live camera, not for photographs. For a plain monochrome
rendering ask for `hires`.

### `video`

A video file with its soundtrack, played until it ends.

```toml
[[scenes]]
type = "video"
name = "The Clip"
display = "mhires"
file = "~/Videos/clip.mp4"
start_s = 0.0
```

*Needs the `video` extra, and the `yt` extra as well for a URL that is a page
rather than a media file. `file` defaults to `assets/videos/`. Display modes:
`mhires` (the default), `hires`, `hires_edges`, `mcm`, `petscii`, `blank`.*

The recognised extensions are `.mp4 .avi .mkv .mov .webm .m4v`.

The soundtrack is the master clock: each frame is chosen against the audio
position rather than a timer, so the two cannot drift apart over a long clip.
On setup the audio is scanned for its peak and the whole track scaled so that
peak lands near full scale — without it a quiet clip plays as silence and
clicks, because the 4-bit DAC has no dynamic range to spare. There is no knob
for that; it always happens.

`file` also accepts a single URL. A direct media link is opened as-is; a page
on a video site is resolved by yt-dlp, which is the `yt` extra. A `t=` or
`start=` or `#t=` timestamp on the URL fills `start_s` for you, so a link
copied at a moment starts there.

```toml
file = "https://youtu.be/<id>?t=18m18s"   # from 18:18
```

`start_s` seeks to the keyframe at or just before the given second, so its
accuracy is keyframe-granular. To loop one video forever, make it the only
scene; to play it once and exit, set `[playlist].loop = false`.

### `waveform`

A SID tune played on the Commodore's own chip, with a three-voice
oscilloscope drawn from the chip's registers.

```toml
[[scenes]]
type = "waveform"
name = "SID Jukebox"
file = "~/Music/hvsc/**/*.sid"
song = 0
color_mode = "per_waveform"
time_base = "auto"
auto_cycles = 4.0
persistence = "short"
```

*Needs no extra — the host-side emulator is a core dependency. `file` defaults
to `assets/sids/`. Bitmap-only: `display` is ignored.*

The tune is not emulated and streamed as audio: the tune and a small player
program are written into the machine's memory and started, and the Commodore
plays it. A host-side emulator runs the same code in parallel to know what the
registers hold, which is what the scope draws — so the picture costs no extra
traffic on the link. The full account is Chapter 4.

The scene is bitmap-only and ignores `display`. The default directory is
`assets/sids/`. PSID files are accepted; RSIDs, and any file that would load
low enough to overwrite the visualiser's bitmap, are refused at setup, and
with a directory pool a refused candidate is skipped and another drawn.

`color_mode = "per_voice"` gives each voice a fixed colour from
`voice_colors`; `per_waveform` colours by what each voice is currently doing,
so the picture changes as the music does. `time_base = "wallclock"` gives one
frame per row; `"auto"` sizes each voice's window so that `auto_cycles`
complete cycles fit, which holds a stable waveform on screen instead of a
sliding one. `persistence` leaves a decaying trail, and `scroll_columns`
turns the strip into a scrolling FIFO rather than a redraw.

Leave `duration_s` out and a loaded song-length database gives each tune its
real length. SHIFT moves to the next subtune on a multi-song file, rebuilding
the emulator and resetting the duration; subtunes under five seconds are
skipped while cycling, because most of those are a game's sound effects and
the scope of one is flat.

### `webcam`

A live camera, quantised to the Commodore in real time.

```toml
[[scenes]]
type = "webcam"
name = "Live"
display = "petscii"
style = "default"
duration_s = 45.0
```

*Needs no extra to run, and the `camera` extra to name a camera by name or USB
identifier rather than by index. Takes no files. Display modes: `hires_edges`
(the default), `hires`, `mhires`, `mcm`, `petscii`, `blank`.*

Choose the camera with `-d` — an index, part of the camera's name, or its USB
identifier — and `c64cast --list-devices` prints what it can find. Frames are
pushed through with no delay buffer, always the newest one, because latency is
what makes a live camera feel live. One camera is shared between the webcam
scene and the gesture controller, so both can run at once.

Every display mode accepts a webcam, and the choice is the whole character of
the scene: `petscii` builds the picture from the machine's own glyphs and is
what people find most charming, `hires_edges` is the default and feels alive
even when frames are stale, `mhires` carries the most colour. Chapter 3
covers the modes and the `style` field's nine PETSCII looks.

The per-source adaptive colour fit does not apply here — it needs to pre-scan
a source, and a live camera has no future to scan.

### `wled`

Turns the Commodore into a virtual LED matrix, receiving a realtime pixel
stream from lighting software.

```toml
[[scenes]]
type = "wled"
name = "LED Matrix"
display = "mhires"
sink_width = 320
sink_height = 200
duration_s = 0.0
```

*Needs no extra — the sink speaks plain UDP. Takes no files. Display modes:
`mhires` (the default), `hires`, `hires_edges`, `mcm`, `petscii`.*

A sender on the network — LedFx, xLights, Jinx!, Glediator, or another WLED
device with sync enabled — streams frames over UDP; c64cast assembles them
into an ordinary frame and hands it to the display pipeline, so it dithers and
quantises exactly like a camera would. Both DDP (port 4048) and the WLED
realtime protocol (port 21324) are bound at once and detected per packet.

`sink_width` and `sink_height` **must match** the matrix the sender is
configured for; the display mode does the downscaling to the Commodore's grid.
There is no audio and no SID. This is one of three WLED directions, and the
other two are `[wled]` settings rather than scenes; Chapter 6 has all three.

## Overlays

An overlay is a decoration attached to a scene, and several may be stacked:

```toml
[[scenes]]
type = "webcam"
display = "petscii"
duration_s = 45.0

  [[scenes.overlays]]
  type = "marquee"
  text = "CCUG MEETUP // 6PM // MAIN HALL"
  row = 0
  fg_color = "yellow"

  [[scenes.overlays]]
  type = "clock"
  corner = "top-right"
```

They paint after the scene and in declaration order, so a later overlay wins
the cells it shares with an earlier one. Placement is yours to keep sane: two
overlays given the same row or the same corner will fight over it, and nothing
warns.

Text overlays paint through a text surface the scene provides rather than
poking screen memory themselves, which is why they work on the bitmap modes as
well as the character ones — the glyphs are folded into the bitmap before it
is sent, so they ride the same path as the picture and stay crisp. On `mhires`
text is always double-width (an 8-pixel glyph spans two of that mode's
4-pixel cells), and `text_double_height` on the *scene* stretches it
vertically as well, for legibility across a room.

The glyphs are the machine's own character ROM, which c64cast reads off your
Commodore on its first run against it and caches. If none can be resolved it
falls back to a rendered ASCII font: readable, but not the C64 font, and
PETSCII graphics characters come out blank. A scroller that looks wrong or
blocky is usually this, and `c64cast --doctor` says which ROM is in use.

### The Overlays

Alphabetically. Parameters and defaults are Appendix C.

**`big_text`** — demo-scene scrolling text at eight times normal size, one
source character filling an 8×8 block of cells. Blank and `mcm` scenes only.

```toml
  [[scenes.overlays]]
  type = "big_text"
  row = "middle"
  speed_cells_per_s = 8.0
  messages = [
    { text = "C64CAST",          color = "rainbow" },
    { text = "GREETINGS TO CCUG", color = "cyan"   },
  ]
```

With `loop = true`, the default, the messages cycle for as long as the scene
runs. With `loop = false` each message plays once and the scene's handover
waits for the last one to leave the screen.

**`callsign`** — one fixed string in a corner: a booth name, a sponsor, an
amateur callsign. Painted once, then costs nothing until something else
changes it.

**`clock`** — the time, and optionally the date, in a corner. `format` and
`date_format` are `strftime` templates.

```toml
  [[scenes.overlays]]
  type = "clock"
  corner = "top-right"
  format = "%H:%M"
  show_date = true
```

**`countdown`** — time remaining to an ISO 8601 `target`, in a corner.
`format = "auto"` picks sensible units as the target nears, or supply a
template of `{d}`, `{h}`, `{m}` and `{s}`. `done_text` shows once it passes.

**`logo`** — a block of PETSCII art from a `.txt` file, one screen row per
line, anchored at a `corner` or at an explicit `row` and `col`. Art wider than
the mode's text grid clips.

**`marquee`** — one row, one string, scrolling continuously.

```toml
  [[scenes.overlays]]
  type = "marquee"
  text = "C64CAST // COMMODORE KEY PAUSES // CTRL SKIPS"
  row = 0
  speed_cells_per_s = 3.0
```

**`network`** — the host's address, its name, and the round-trip time to the
Commodore, in a corner. `items` chooses which of `ip`, `hostname` and `ping`
appear. Polled on a background thread.

**`obs_status`** — the current OBS Studio scene and its dropped-frame count,
read over the OBS WebSocket. Needs the `obs` extra, and OBS's WebSocket server
enabled under Tools.

**`rss`** — a marquee fed by a feed. Headlines are fetched on a background
thread every `refresh_minutes` and joined with `separator`.

```toml
  [[scenes.overlays]]
  type = "rss"
  url = "https://news.ycombinator.com/rss"
  row = 1
  max_items = 8
```

**`scrolling_text`** — one row cycling a list of messages, each with its own
colour, and optionally its own pause, delay, or a `static` style that holds
still instead of scrolling.

```toml
  [[scenes.overlays]]
  type = "scrolling_text"
  row = 24
  speed_cells_per_s = 6.0
  messages = [
    { text = "WELCOME", color = "yellow" },
    { text = "73", color = "white", style = "static" },
  ]
```

**`spectrum_bitmap`** — the audio spectrum as bars a scanline high, folded
into the multicolor bitmap. `mhires` only, where it is the right choice: it
has 200 levels of bar height rather than 25, and it takes only the one colour
slot per cell that it needs, leaving the picture underneath its other three.

**`spectrum_petscii`** — the same eight bands as coloured cells, for the
character modes. `placement` puts the strip at the bottom, the centre, or
splits it above and below.

**`weather`** — temperature and conditions in a corner, polled in the
background. `open-meteo` takes `lat` and `lon`; `wttr.in` takes a `location`
name.

Both spectrum overlays read the scene's own music features first and fall back
to analysing the audio stream, which is why they work on a SID scene where
there is no audio stream at all — the chip is making the sound.

### `big_text` Wants the Scene to Itself

It scrolls by moving the VIC's horizontal scroll register and flipping between
two pages of screen memory. Both are whole-screen effects: any other overlay
sharing the scene is dragged sideways by the scroll and blinks out on the
frames that show the other page. Give `big_text` its own scene, and put the
clock on the next one.

## Choosing a Display Mode for an Overlay

Appendix D is the matrix. A cell in it is refused at configuration time, not
at the moment it would have drawn, so a bad pairing is a message before the
run rather than a blank corner during it.

Every refusal is one of three rules:

**Text needs somewhere to put characters.** The text overlays work on
`petscii`, `blank`, `hires` and `mhires`. They refuse `mcm`, which is the
non-obvious one: that mode uses the high bit of colour memory to mark a cell
as multicolor and halves the horizontal resolution, so neither a character
glyph nor a folded bitmap glyph lands where it should.

**A few overlays are written against one mode's memory layout.**
`spectrum_bitmap` is `mhires` only, and `big_text` is `blank` and `mcm` only —
on a PETSCII scene it would stomp the live frame's own glyphs.

**Some overlays need sound to exist.** The spectrum pair will build without
it, and simply paint nothing.

When a combination is refused, there is nearly always a neighbour that works:

| You wanted | Do this instead |
|---|---|
| A clock over an `mcm` scene | Use `mhires` for a comparable colour budget, or `petscii` |
| A spectrum over `mhires` | `spectrum_bitmap` — the same overlay, native to that mode |
| `big_text` and a clock together | Two scenes, one each |
| A spectrum over a video | `mhires` with `spectrum_bitmap`, or a `petscii` video with `spectrum_petscii` |

`c64cast --compat` prints the matrix at the terminal, and `--doctor` reports
an incompatible pairing against your actual configuration, naming the scene.
