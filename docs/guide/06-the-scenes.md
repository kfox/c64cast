---
number: 3
---

# The Scenes

A scene is one thing on the screen. This chapter walks through every kind
c64cast can run, starting with the ones that need nothing and working up to
the ones that need other equipment. Each section shows the smallest
configuration that works, and says what the scene is genuinely good at.

You do not need to read this chapter in one sitting. Find the scene you want,
try it, come back later for another.

> [!TIP]
> Every scene here has a ready-made demonstration shipped inside c64cast, one
> per scene type — `python -m c64cast --list-examples` names them all. Run any
> of them directly: `python -m c64cast --config example:scene-waveform`.
> Because each defines exactly one scene, it loops forever until you stop it.

## Blank

The simplest scene. It paints a solid canvas in the colours you choose and
does nothing else.

```toml
[[scenes]]
type = "blank"
name = "Title Card"
duration_s = 12.0
border = "black"
background = "blue"
```

On its own this is a coloured rectangle. Its purpose is to be a backdrop:
every overlay that works on character modes works here, so a blank scene
plus a scrolling message is a title card, and a blank scene plus a clock and
the weather is an information board. Chapter 4 covers overlays properly.

Colours may be given as names or as numbers from 0 to 15. Names are matched
loosely, so `"light green"` and `"lightgreen"` both work.

## Slideshow

Still images, fitted to the screen and dithered into the C64's palette.

```toml
[[scenes]]
type = "slideshow"
name = "Photographs"
display = "mhires"
file = "~/Pictures/*.jpg"
duration_s = 60.0
image_duration_s = 5.0
```

Two durations, doing different jobs: `image_duration_s` is how long each
picture stays up, and `duration_s` is how long the whole scene runs before
the playlist moves on. The example shows twelve photographs, five seconds
each.

The `file` setting appears on several scene types and always means the same
thing: a comma-separated list of files, directories and glob patterns, whose
union forms a pool. Point it at a directory and you get everything in it.

```toml
file = "~/Pictures/holiday.jpg"                 # one picture
file = "~/Pictures"                             # a whole directory
file = "~/Pictures/*.png"                       # a pattern
file = "~/Pictures, ~/Downloads/*.jpg"          # both
```

Images are not shown in a fixed order. c64cast shuffles the pool and walks
through it, so every picture appears once before any repeats, and none ever
appears twice in a row.

## Video

A video file, with its soundtrack, played until it ends.

```toml
[[scenes]]
type = "video"
name = "The Clip"
display = "mhires"
file = "~/Videos/clip.mp4"
```

Note the absence of `duration_s`. A video scene runs for exactly as long as
the video does, and setting a duration on one is rejected rather than
quietly ignored, because the two ideas conflict.

Video is where c64cast works hardest. Each frame is decoded, scaled, colour
corrected and quantized into whatever the display mode allows, then written
to the Commodore. The soundtrack is decoded in parallel and played through
the sound chip, and the video is paced off the audio clock rather than a
timer, so the two cannot drift apart over a long clip.

Web links work as file paths. Direct links to media play immediately; links
to video sites are resolved first, which needs the optional `yt` feature
installed. If the link carries a timestamp, playback starts there.

## Waveform

A SID tune, played on the Commodore's own sound chip, with a three-voice
oscilloscope drawn from the chip's registers.

```toml
[[scenes]]
type = "waveform"
name = "SID Jukebox"
file = "assets/sids"
duration_s = 180.0
color_mode = "per_voice"
voice_colors = ["cyan", "yellow", "light green"]
```

This is one of the most satisfying things c64cast does, and it is worth
being clear about what is happening. The tune is not being emulated on your
computer and streamed as audio. c64cast writes the tune and a small player
program into the Commodore's memory and starts it. The Commodore plays the
music itself, from its own chip. Meanwhile c64cast reads the state of the
three voices and draws them.

![Figure 3-1. Three voices of a SID tune, drawn from the chip's own registers.](img/fig-3-1-waveform.png)

Point `file` at a directory and each time the scene starts it picks a tune at
random. Leave `duration_s` out and, if you have the SID song-length database,
c64cast plays each tune for its actual length.

`color_mode` decides what the colours mean: `per_voice` gives each voice its
own fixed colour, and `per_waveform` colours by the waveform each voice is
currently using, so the picture changes as the music does.

### Getting Some Tunes

c64cast ships no music. The place to get some is the **High Voltage SID
Collection**, a decades-long archival effort that has collected tens of
thousands of tunes spanning the C64's entire history. It is free, and it is
the definitive source:

[hvsc.c64.org](https://www.hvsc.c64.org/)

Download the archive and extract it into an `assets/sids/` directory,
alongside the c64cast files. A `file` setting will happily point anywhere you
like, but that particular directory is worth using: it is where c64cast looks
by default for the song-length database described below. Then point a
waveform scene at it and let it pick:

```toml
[[scenes]]
type = "waveform"
name = "SID Jukebox"
file = "assets/sids"
```

> [!TIP]
> Extracting the whole collection also gets you its song-length database,
> which lists how long every tune actually runs. c64cast finds it
> automatically under `assets/sids/`, and once it has, you can leave
> `duration_s` off entirely and each tune will play for its real length
> instead of being cut off at an arbitrary number of seconds.

> [!NOTE]
> The waveform scene needs a bitmap display mode, and it needs the Web
> Remote Control Service from Chapter 1, because starting the player is a
> different kind of operation from painting pixels.

## Generative

Pictures computed from nothing, optionally reacting to music.

```toml
[[scenes]]
type = "generative"
name = "Plasma"
display = "mhires"
source = "plasma"
duration_s = 60.0
effect = "trails"
```

A generative scene is built from three independent choices. The **source**
is the pattern: about twenty of them, including `plasma`, `tunnel`, `fire`,
`mandelbrot`, `metaballs`, `rotozoomer`, `game_of_life` and `fireworks`. The
**effect** is an optional filter over the result: `trails`, `pulse`,
`rgb_shift` or `blur`. The **audio source** decides whether the picture
reacts to anything.

![Figure 3-2. A plasma field, quantized to four colours per cell.](img/fig-3-2-generative.png)

Setting `audio_source` turns a pretty pattern into a music visualizer:

| `audio_source` | What happens |
|---|---|
| `none` | Silent, and the pattern runs on its own clock. The default |
| `file` | Play an audio file through the C64, and react to it |
| `sid` | Play a `.sid` tune on the real chip, and react to it |
| `mic` | Play live microphone input through the C64, and react to it |
| `listen` | React to live input, but send no audio to the C64 |

`file` and `sid` both need a `file` setting naming what to play. This is what
happens behind the scenes when you hand c64cast an MP3 on the command line:
a reactive plasma over your track.

## Launcher

Hand the machine over to a real Commodore program, then take it back.

```toml
[[scenes]]
type = "launcher"
name = "Games"
file = "assets/programs"
duration_s = 120.0
reset_before_launch = true
```

c64cast resets the Commodore for a clean start, uploads the program and runs
it. From that moment the Commodore is simply a Commodore running a game or a
demo; c64cast is not drawing anything.

`duration_s` here is not a time limit but an **idle timeout**. c64cast
watches for keyboard and joystick activity, and only reclaims the machine
once that long has passed with no input. Someone playing a game is not
interrupted mid-level. Set `max_duration_s` if you do want a hard ceiling,
and `min_duration_s` to guarantee a minimum before the idle timer can fire.

## Webcam

A live camera, quantized to the C64 in real time.

```toml
[[scenes]]
type = "webcam"
name = "Live"
display = "petscii"
duration_s = 45.0
```

![Figure 3-3. A live camera feed rendered as PETSCII characters.](img/fig-3-3-webcam.png)

Choose the camera with `-d` on the command line: a number, or part of the
camera's name, or its USB identifier. `python -m c64cast --list-devices`
prints everything it can find.

`display = "petscii"` builds the picture out of the Commodore's own
character set, choosing a glyph by brightness and a colour by hue. It is the
mode people find most charming, and it is fast, because character modes move
far less data than bitmaps do.

## MIDI

Play the real SID chip from a keyboard.

```toml
[[scenes]]
type = "midi"
name = "SID Synth"
duration_s = 300.0
```

Connect a MIDI controller, and c64cast turns what you play into SID register
writes, with the same three-voice oscilloscope as the waveform scene. The
Commodore becomes a three-voice synthesizer that you play directly. This
needs the optional `midi` feature installed.

## ASID

Receive a SID stream from elsewhere on the network and play it on the real
chip.

```toml
[[scenes]]
type = "asid"
name = "Incoming"
duration_s = 300.0
```

ASID is a small protocol for sending SID register writes over MIDI. Several
programs speak it: the DeepSID website in a browser, the SIDFactory II
tracker, and various software synthesizers. Point one of them at c64cast and
whatever it plays comes out of your Commodore's actual sound chip, with the
oscilloscope drawn alongside. Composing on a tracker while a real SID plays
your work back is a genuinely different experience from hearing an emulator.

## WLED

Turn the Commodore into a video wall for lighting software.

```toml
[[scenes]]
type = "wled"
name = "LED Matrix"
display = "mhires"
duration_s = 120.0
```

This scene makes c64cast pretend to be an LED matrix. Lighting programs such
as LedFx and xLights stream pixels to it over the network in the usual
formats, and c64cast paints them on the Commodore's screen. It is the
inverse of the more common arrangement, and it is covered along with the
rest of the LED integration in Chapter 5.

## Choosing Between Them

If you are not sure where to start:

- For something impressive with no preparation, try **waveform** with a
  directory of SID tunes.
- For something that looks good on a shelf all day, a **slideshow** of
  photographs with a clock overlay.
- For a party, **generative** with `audio_source = "mic"`.
- For a demonstration to someone who knows what a Commodore is, **video**,
  because the disbelief is worth watching.

The next chapter is about making any of them look better.
