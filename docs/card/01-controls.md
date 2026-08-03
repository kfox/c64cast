# Controls

Every surface here sets the same flags: a skip from the keyboard, an HTTP
request, a pad and a gesture are indistinguishable downstream.

## The Commodore's Keyboard

| Key | Running | Paused |
|---|---|---|
| <kbd>C=</kbd> | Pause | Hold 3 s to resume |
| <kbd>CTRL</kbd> | Skip to the next scene | — |
| <kbd>SHIFT</kbd> | Cycle style, mode, overlays | — |
| <kbd>SPACE</kbd> | Open the on-C64 menu | — |

Chords resolve rather than combine: C= plus CTRL is a pause, and SHIFT
alongside either is ignored. The menu needs `[menu].enabled`, a backend that can
read memory, and takes the cursor keys while it is open.

## Gestures

`[vision].enabled`, over the shared camera.

| Gesture | Running | With `[vision].performance` |
|---|---|---|
| Pinch | Pause; hold to resume | Hold: toggle effect layer 1 |
| Swipe | Skip | Next clip |
| Open hand | Cycle style | Hold: toggle effect layer 2 |

Pinch-to-resume is unchanged in both modes, so a paused show always recovers the
same way.

## MIDI: What Ships Mapped

`[midi_control].enabled`, no `cc_map` written. Sized for a 16-pad grid with a
knob bank.

| Message | Action |
|---|---|
| Notes 36–39 | Skip, cycle style, pause/resume, jump to scene 0 |
| Notes 40–55 | Jump to scenes 0–15 |
| Program change 0–15 | The same bank, for foot controllers |
| CC 13–16 | `effect.decay`, `source.speed`, `source.scale`, `source.scroll_speed` |

CC 1, 7 and 71–75 are left free for the `midi` scene's own synth controls.
Nothing is mapped to Record. Precedence is these defaults, then a learned
profile, then your own `cc_map`.

## MIDI: The Actions

| Group | Actions |
|---|---|
| Show | `pause`, `resume`, `toggle_pause`, `skip`, `cycle_style`, `jump` |
| Parameters | `param`, `fx_toggle` |
| Video | `transport.play_pause`, `transport.stop`, `transport.loop_toggle`, `transport.rw`, `transport.ff`, `transport.jog`, `transport.record`, `loop_slot` |
| Performance | `clip_launch`, `tempo_tap`, `look_save`, `look_recall` |
| Feedback | `osd.position` |

A `cc_map` entry is a `type` (`note`, `cc`, `pc` or `mmc`), a `number`, an
`action`, and for `param` a `target`.

## Pad Chords

Scrubbing a video, `rw` and `ff` double their speed every three-quarters of a
second, up to thirty times real time. `loop_toggle` marks A, then B, then
releases.

| Press | Does |
|---|---|
| `loop_slot` | Recall that loop |
| `loop_slot` + **Stop** | Save the current loop into it |
| `loop_slot` + **Record** | Clear it |

MMC frames have no release, so they cannot hold a chord. Map these to notes.

## Pad Lights

`[performance].midi_feedback` opens an output port and colours a pad with a
note-on at its own number: Launchpad, APC, MPC and Push. Arturia's proprietary
lighting is not driven; use the console instead.

| Light | Means |
|---|---|
| Bright | Playing |
| Blinking | Armed |
| Dim | Loaded |
| Lit | Effect layer on |

## Live Parameters

A `param` target is `holder.name`. A knob scales into the parameter's range;
over a fixed set of values it selects across the set, and a pad steps through
it. A target the running scene does not have is a silent no-op, so a whole knob
bank can stay mapped across a mixed playlist.

| Holder | Reaches |
|---|---|
| `source` | The generative scene's generator |
| `effect` | The first effect layer |
| `fx0`, `fx1`, … | One specific layer |
| `scene` | The scene itself |
| `mode` | The display mode |
