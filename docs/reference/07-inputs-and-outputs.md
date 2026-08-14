---
number: 6
---

# Inputs and Outputs

A running show is not a closed loop. Keys are pressed, cameras and microphones
feed it, controllers and phones drive it, LED fixtures react to it, a recorder
captures it, and in an ensemble several Commodores have to agree. This chapter
is everything that reaches c64cast from outside, and everything it sends back
out.

Every control surface here is interchangeable. A skip from the Commodore's
keyboard, an HTTP request, a MIDI pad and a hand gesture set the same flag and
are indistinguishable downstream — which is why a capability added to one
arrives on all of them.

## The Machine's Own Keyboard

The one input that comes from the Commodore itself. c64cast polls the kernal's
keyboard scratch bytes ten times a second — which is why the BASIC program of
Chapter 5 has to keep running underneath every scene — and reads three
modifiers, plus <kbd>SPACE</kbd> when the on-C64 menu is enabled:

| Key | While running | While paused |
|---|---|---|
| <kbd>C=</kbd> | Pause: the scene tears down and the machine idles | Held for 3 s, resume |
| <kbd>CTRL</kbd> | Skip to the next scene | Nothing |
| <kbd>SHIFT</kbd> | Cycle the style of the scene, the display mode and every overlay | Nothing |
| <kbd>SPACE</kbd> | Open the on-C64 menu, with `[menu].enabled` | Nothing |

Chords are resolved rather than combined. C= and CTRL in the same tick means
pause, with the skip dropped; SHIFT held alongside either is ignored, because a
thumb resting on shift while reaching for pause should not also change the look.

A failed read is treated as "could not tell" rather than as "not pressed", so a
dropped packet never phantom-resets a held-key timer.

What SHIFT cycles is each surface's own business, and each returns a label that
lands in one log line: the PETSCII styles rotate, a waveform scene advances to
the next subtune, a `big_text` overlay rotates its color. A surface that does
not opt in does nothing.

### The On-C64 Menu

With `[menu].enabled`, <kbd>SPACE</kbd> on the real keyboard opens a panel of
context-sensitive knobs for the running scene — display mode, palette mode,
style, the scope's settings — navigated with the cursor keys and RETURN, and
closed with SPACE again. Changes apply live, so the screen behind the panel is
the preview.

While the menu is open the C= / CTRL / SHIFT controls are suspended and the
cursor keys drive the menu instead. On exit with unsaved changes,
`prompt_to_save` decides whether you are offered the chance to write them back
into the configuration you launched from; `false` applies them to the running
scene and never persists, which is what a convention stand wants.

Which knobs the panel offers is a property of the key rather than of the menu:
the ones Appendices A and B mark *menu-live* are the ones a running scene can
take a change to in place. Everything else would need the scene rebuilt, so it
is not in the panel.

It needs a backend that can read memory — the Ultimate, or a cycle-clean
TeensyROM+ — because SPACE is not a modifier and has to be read out of the
kernal's keyboard buffer. Text-valued parameters are shown read-only.

### Saving What a Run Changed

Two surfaces can move a setting mid-show, and both can offer the change back
to the file you launched from. They do it at different moments.

The menu offers on the spot. It mutates the running configuration as you turn
a value, and `prompt_to_save` decides whether closing it with unsaved changes
asks you anything.

The MIDI surface offers at the end. Every display-mode parameter it moves —
the `mode.*` targets of Appendix F — is recorded as it happens, and c64cast
offers the result once the run is over and the terminal is free. What it
records is the net change rather than the journey, so a knob swept out and
brought back leaves nothing behind, and a parameter tuned five times leaves
one entry.

What happens at exit depends on what the run had:

| The run | At exit |
|---|---|
| A configuration file, on a terminal | The changes are listed and you are asked whether to save them |
| A configuration file, with `--overwrite` | They are applied and saved, with no prompt |
| A configuration file, with no terminal to ask on | They are listed, nothing is written, and you are told to re-run with `--overwrite` |
| No configuration file — quick playback | There is nothing to write to, so a pasteable `[color]` block is printed instead |

It runs after a normal exit and after an interrupt at the terminal alike, and
in an ensemble each system is offered separately, tagged with its name.

Only the six parameters that have a `[color]` field to land in are ever
written: `dither_strength`, `dither_method`, `color_match`, `cell_strategy`,
`motion_smoothing` and `auto_fit_strength`. A scene's `palette_mode`, a
generator's `speed`, an effect's `decay` — all of them move live, and none of
them is configuration the `[color]` section can hold, so none is offered back.

> [!WARNING]
> Saving rewrites the whole configuration file from the settings in memory,
> not just the lines that changed. The values survive; comments, key order and
> spacing are replaced by the serializer's own. The file it replaces is kept
> as `<name>.bak` — one deep, so a second save's backup is the first save's
> output rather than what you originally wrote.

## Cameras and Microphones

### Choosing a Camera

`-d/--device`, or `[video].device`, takes three forms:

```toml
[video]
device = 0                  # an OpenCV index
device = "Cam Link"    # a case-insensitive substring
                       #   of the camera's name
device = "0fd9:0066"        # a USB vendor:product identifier
```

An index is the fragile one: it changes when devices are plugged in, and
sometimes across a reboot. A name or an identifier is matched against enumerated
cameras and opened with the interface it was enumerated with — which matters,
because an enumerated index is only valid for the interface that produced it.
Matching by name or identifier needs the `camera` extra; without it, an index
still works.

```bash
c64cast --list-devices
```

prints what can be found, cameras and audio inputs both, with identifiers and
indices. An ambiguous name warns and takes the first match; a name that matches
nothing is an error naming the candidates.

**One camera is shared.** The webcam scene and the gesture controller read
through a single broker that owns the capture and hands out copies of the newest
frame, so both can run at once and neither queues stale frames. That is also why
a webcam scene has no latency buffer: it always gets the freshest frame, and a
missed one is simply skipped.

### Choosing a Microphone

`-D/--audio-device`, or `[audio].device`, takes an index or a name substring —
no identifier, since the audio layer exposes none. Unlike the camera resolver
this one never fails: a name matching nothing warns and falls back to the system
default input.

That forgiveness has a cost worth knowing about during DAC calibration, where
the system default is usually the built-in microphone and a calibration measured
off room noise fails expensively. The calibrator therefore looks for a capture
device by name first, and warns immediately when it has fallen back — five
seconds into the run rather than fifty.

`--save-settings` persists whichever devices you chose, so they need not be
retyped; see Chapter 1.

## MIDI In and Out

Two entirely separate MIDI surfaces exist, and they open separate ports.

The `midi` **scene** of Chapter 2 turns the Commodore into an instrument. The
`[midi_control]` **listener** is a service running for the whole process, alive
across every scene, and it drives the show rather than the sound. MIDI ports are
exclusive opens, so feeding both from one controller means routing it to two
virtual ports, or enabling MIDI Thru at the operating system.

### The Vocabulary

A `[[midi_control.cc_map]]` entry maps a message to an action. The message is a
`note`, a `cc`, a `pc` (program change) or an `mmc` (a transport
system-exclusive frame). The actions fall into five groups:

| Group | Actions |
|---|---|
| Transport of the show | `pause`, `resume`, `toggle_pause`, `skip`, `cycle_style`, `jump` |
| Live parameters | `param`, `fx_toggle` |
| Transport of a video | `transport.play_pause`, `transport.stop`, `transport.loop_toggle`, `transport.rw`, `transport.ff`, `transport.jog`, `transport.record`, `loop_slot` |
| Performance | `clip_launch`, `tempo_tap`, `look_save`, `look_recall` |
| Feedback | `osd.position` |

A `param` entry names its target as `holder.name`:

```toml
[[midi_control.cc_map]]
type = "cc"
number = 14
action = "param"
target = "source.speed"     # the generator's speed
```

| Holder | Reaches |
|---|---|
| `source` | A generative scene's generator |
| `effect` | The first effect layer |
| `fx0`, `fx1`, … | A specific effect layer |
| `scene` | The scene itself — the oscilloscope's `gain` |
| `mode` | The display mode: `dither_strength`, `dither_method`, `cell_strategy`, `color_match`, `palette_mode`, `motion_smoothing`, `auto_fit_strength` |

Appendix F is the full list of targets. A continuous controller's 0–127 range is
scaled into the parameter's declared range; for a parameter that takes a fixed
set of values instead, a controller selects across the set and a pad steps
through it. A target the current scene does not have is a silent no-op, which is
what makes it safe to leave a whole knob bank mapped across a mixed playlist.

Anything that would need a scene *rebuild* is deliberately absent: a display-mode
switch or a scene-type change costs real setup time, and is categorically wrong
for a control that fires on a beat. Launching a whole scene from a pad is what
the clip grid below is for, and it hides that cost behind a count-in.

### What Ships Mapped

The listener works with no configuration at all. Out of the box, for a typical
16-pad grid with a knob bank:

| Message | Action |
|---|---|
| Notes 36–39 | Skip, cycle style, pause/resume, jump to scene 0 |
| Notes 40–55 | Jump to scenes 0–15 |
| Program changes 0–15 | The same bank, for foot controllers |
| CC 13–16 | `effect.decay`, `source.speed`, `source.scale`, `source.scroll_speed` |

The knob bank deliberately avoids CC 1, 7 and 71–75, which are the `midi`
scene's synth controls, in case one controller feeds both. Nothing dangerous is
mapped by default: neither `transport.record` nor any MMC command has a default
binding, because a default that fires Record on an unrecognized controller is a
worse failure than one that does nothing.

### Driving a Video

The `transport.*` actions turn a video scene into something closer to a deck.
`play_pause` holds the frame in place; `rw` and `ff` scrub while held, at a speed
that doubles every three-quarters of a second up to thirty times real time; `jog`
maps a knob to an absolute position, or decodes an endless encoder as a relative
one.

`loop_toggle` sets a loop the way a looper pedal does: the first press marks A,
the second marks B and starts looping, the third releases it. `loop_slot` pads
persist those loops. A plain press recalls the slot; the same press with **Stop**
held saves the current loop into it, and with **Record** held clears it. The
loops live in a file per video under the data directory, keyed so that moving
the file does not orphan them.

By default audio keeps playing across every splice — seek, pause, loop wrap —
and is re-synchronized rather than muted, so a loop is musical.
`[midi_control].loop_audio = "mute"` restores the older behavior of muting for
the rest of the scene, which is the escape valve if a splice ever misbehaves.

> [!NOTE]
> MMC frames have no release. An MMC-mapped Record or Stop still fires its
> one-shot action, but it cannot drive the pad chords above, which need a real
> note-off. Map those to notes.

### Learning a Controller

```bash
c64cast --midi-setup
```

watches your controller and writes a reusable profile: press the buttons it asks
about, sweep each knob, pick each knob's target from a list generated from the
same registry Appendix F is, and optionally teach it how your grid lights its
pads. Endless encoders are detected by their value pattern and offered as a jog
control. It needs the `midi` and `wizard` extras, and it runs instead of
playback.

`[midi_control].controller_profile` then layers that profile in. `"auto"` picks
the profile whose learned port name matches the port that opened; `"off"` ignores
profiles entirely. Precedence is the shipped defaults, then the profile, then any
`cc_map` you wrote yourself — so a profile can reclaim the default note numbers,
and your own entries always win.

### MIDI Out — Lighting the Pads

The Commodore's screen faces an audience, which makes it the wrong place for
performer feedback. `[performance].midi_feedback` opens an **output** port and
lights the grid's pads instead:

| Pad | State |
|---|---|
| A clip that is playing | Bright |
| A clip that is armed | Blinking |
| A clip that is loaded | Dim |
| An effect layer that is on | Lit |

A pad is colored by sending a note-on at its own number with the color in the
velocity, which is what Novation Launchpad, Akai APC and MPC, and Ableton Push
all do. It is not universal: Arturia controllers drive their pad lights over
proprietary system-exclusive messages, and light nothing here. For those, the web
console below is the intended feedback surface.

The blink is generated on the host rather than asked of the controller, so it
works on any grid, and only pads whose color actually changed are sent — a
static state is silent after the first paint. Every managed pad is extinguished
at shutdown.

### Clock

MIDI clock, start, stop, continue and song-position messages are consumed
straight off the wire and feed the beat grid — see "Performing" below. When the
clock arrives on a different port than the control surface,
`[performance].clock_port` opens a second input for it.

## The Control Plane

With `[control].enabled` and the `control` extra, an HTTP server runs on
`127.0.0.1:8765`:

| Route | Does |
|---|---|
| `GET /status` | The current scene and index, whether it is paused, and the link's write latency |
| `GET /scenes` | The playlist, with each scene's duration and which one is live |
| `POST /pause`, `/resume`, `/skip` | Exactly what the keyboard does |
| `POST /reload` | Re-read the configuration from disk and rebuild the scenes at the next scene boundary |
| `GET /perf` | The performance console |

`/reload` is how a show is edited while it runs: save the file, post, and the new
scenes take effect at the next boundary rather than mid-scene.

Every route takes an optional `?system=` naming one system of an ensemble, or
`all`. Omitted, a single-system run answers for its one playlist and an ensemble
answers for every system at once.

### Locking It

The server answers anyone who can reach the port. On loopback that is the same
audience as your keyboard; bound to a LAN address it is not, which is what
`[control].token` is for:

```toml
[control]
enabled = true
host = "0.0.0.0"
# or $C64CAST_CONTROL_TOKEN, which wins:
token = "a-long-random-string"
viewer_token = "another-one"      # may read, may not touch
```

With a token set, every route needs it — including the console page and its
WebSocket. A script sends it as `Authorization: Bearer …`, as `X-C64Cast-Token`,
or as `?token=…`. A browser can do none of those on a plain navigation, so open

```
http://HOST:8765/api/login?token=a-long-random-string
```

once: it stores the token in a cookie and drops you on the console, and
everything the page does from then on is authenticated. The token defaults to
empty, which is the historical behaviour — open. Prefer the environment variable
to a value in a file you might commit or share.

A `viewer_token` is the same page with the writes removed: reads succeed, pause,
skip, reload and every clip launch are refused, and the console shows a
`read-only` chip so the refusal is visible rather than mysterious.

This is a shared secret over plain HTTP on your own network — a lock on the
door, not a bank vault. It does not make the port safe to expose to the
internet; nothing here does.

### What a Reload Re-Reads

A reload rebuilds **`[[scenes]]` and `[interstitial]`**, and nothing else. The
connection, the audio path, the capture device and the running playlist's own
`loop` and `fade_duration_s` are all established once, at startup, from the
threads and the link that were built out of them; re-reading those would mean
tearing that apart mid-show, so a reload leaves them exactly as they are. To
change one, restart.

A file that fails to load leaves the running playlist untouched and logs why,
so a typo saved mid-show costs a log line rather than the show.

### Signals

The same reload is available with no control plane at all, on any POSIX host:

```bash
kill -HUP $(pgrep -f c64cast)
```

`SIGHUP` re-reads every system's configuration in place, under the rule above.
Windows has no `SIGHUP`, which is why `POST /reload` is the portable spelling of
it. `SIGTERM` stops the run the way a normal exit does, tearing the scene down
and putting the machine back; <kbd>CTRL</kbd> <kbd>C</kbd> still interrupts at
the terminal. A second <kbd>CTRL</kbd> <kbd>C</kbd> while teardown runs does
not kill the process — it restores the default handler, so it is the *third*
press that kills outright. The ladder is deliberate: a kill can cut a DMA
transfer mid-flight, which wedges the hardware and skips the machine's final
reset, so the escalation is there for a teardown that is genuinely stuck, not
an impatient one.

In an ensemble each system re-reads its own file independently, from the path it
was originally loaded from. The master is not re-read, so adding or removing a
system needs a restart.

### The Performance Console

`GET /perf` is a phone-sized touch page served by that same server — no separate
process, no app to install. It shows the clip grid, an effect rack, the tempo
with a tap button, and the saved looks, one panel per system.

It drives the same engine the MIDI surface does, so a launch from a phone and a
launch from a pad are indistinguishable downstream. A touch sends press on
contact and release on lift, so momentary clips work by touch exactly as they do
from a pad, with no per-launch-type handling in the page.

The effect rack's rows are generated from each live layer's own declared
parameters, so the rack cannot drift from what is actually loaded. State is
pushed over a WebSocket a few times a second, and the beat indicator is
extrapolated on the page between pushes, so it animates smoothly without a round
trip per beat.

Nothing on the console writes to the on-screen display. Performance feedback
stays off the audience's screen.

## The Web Console Host

`c64cast --serve` (or `[web].enabled`, the same switch, with the `web` extra)
changes what the program *is*. Instead of running one configuration and exiting,
it becomes a server that holds the Commodore and starts and stops shows on
request:

```toml
[web]
enabled = true
host = "127.0.0.1"
port = 8123
autostart = false      # start the launched config immediately
settle_s = 3.0         # hardware cool-off between shows
```

Everything the control plane serves rides on that same port — `/status`,
`/reload`, the performance console — so there is one address, not two. Between
shows those routes answer `503`: the machine is idle, not broken.

| Route | Does |
|---|---|
| `GET /api/session` | Where the host is: `idle`, `starting`, `running`, `stopping` or `error`, with the current systems, the last error and a tail of the log |
| `POST /api/session/start` | Build and run the configuration the host was launched with |
| `POST /api/session/stop` | Bring the running show down and put the machine back |
| `POST /api/session/switch` | Stop, wait for the hardware, and start again — re-reading the file |
| `POST /api/session/reload` | The same reload the control plane offers |
| `GET /api/introspect` | Every configuration section, scene type, overlay, display mode and live target, as JSON |
| `WS /api/ws` | Live state: the performance payload, the session state, and new log lines as they happen |

The configuration is re-read from disk on every start, so editing the file and
posting `start` again runs the edit — no restart of the host. Starting takes
several seconds (opening the link, resetting the machine, probing what it has),
so `start` and `switch` answer `202 Accepted` immediately and the WebSocket
reports what happened. A start while something is already running is refused
with `409` rather than silently replacing it; that is what `switch` is for. A
configuration that will not run is refused with `422` **before** anything
touches the machine, so a typo costs a response, not a show.

After one show ends the next start waits out `settle_s` seconds. This is not
politeness: the Ultimate's DMA service refuses new connections for a few seconds
after one closes, and a camera will not reopen instantly either.

### The Token Is Not Optional Here

Unlike the control plane, this surface has no unauthenticated mode. It starts
and stops hardware, so if no token is configured one is generated, stored
`0600` under the data directory, and printed at startup as a URL you can open:

```
web console: open
  http://127.0.0.1:8123/api/login?token=…&next=/perf
```

Set `[web].token` (or `$C64CAST_WEB_TOKEN`, which wins) to choose your own, or
`token_file` to keep it out of the configuration entirely. `viewer_token` grants
the same read-only role the control plane's does: watch the state feed, but
never start, stop or edit.

### Living Through a Crash

A host writes a small marker file while a show runs and removes it on the way
down. If it finds one at the next start, the previous run died with the machine
still mid-show — so before building anything it opens a bare connection, resets
the Commodore, and closes it again. That makes a host under `launchd` or
`systemd` strictly safer than the one-shot command, which has no second chance
at that reset.

A preview window under `--serve` works from a terminal but is not a supported
way to run one; a browser-side preview is the intended answer and is still to
come.

## Performing

Three pieces sit on top of the playlist: a beat grid, a clip-launch grid, and the
effect chain. They are configured in `[performance]` and `[[performance.clips]]`.

### The Beat Grid

One tempo, several consumers. `[performance].tempo_source` picks where it comes
from:

| Value | The grid follows |
|---|---|
| `"internal"` | The static `[performance].bpm`, re-anchored by a `tempo_tap` pad |
| `"midi"` | An external MIDI clock — a DAW, a drum machine |
| `"audio"` | The tempo the live-input analyzer detects, on a `mic` or `listen` scene |

The grid's phase is integrated from the tempo rather than snapped to individual
events, so it is monotonic and never jerks backward when a clock byte arrives
late. In `"audio"` mode a silent input freezes the grid rather than growing a
phantom tempo out of noise.

Three things consume it: launch quantization below, effects with `mod_source =
"clock"`, and — with `broadcast_tempo_fallback` — the WLED broadcast on scenes
that have no music of their own.

### The Clip Grid

A clip is a scene fired from a pad, quantized to the grid. It takes every key an
ordinary scene takes, plus how it launches:

```toml
[[performance.clips]]
slot = 1
pad = 60
type = "generative"
source = "tunnel"
display = "mhires"
launch = "trigger"
quantize = "bar"
```

| `launch` | Behavior |
|---|---|
| `trigger` | Plays through, then loops |
| `gate` | Plays while the pad is held, and restores what it interrupted on release |
| `toggle` | Latches on and off |

| `quantize` | Fires |
|---|---|
| `off` | At once |
| `beat` / `bar` | At the next beat or bar |

Building a scene costs real setup time — opening a decoder, resolving a URL — so
a press starts that work immediately, in the background, and the count-in to the
quantization boundary hides it. A stopped clock fires at once, so a pad always
does something.

A launch remembers what it interrupted, one level deep, which is what makes
"gate a stab over a running loop, release, land back in the loop" work. A clip
declares its own pad, so it needs no separate mapping line.

### Looks

A *look* is the active clip plus the on-screen scene's whole effect-chain state:
which layers are bypassed, what each parameter is set to, and what each is
modulated by. `look_save` captures one to a slot; `look_recall` re-fires it,
re-arming the clip and applying the effect state once the swap lands. The web
console does the same with a SAVE toggle beside the pads.

A look recalled onto a differently-built chain applies what it can, by layer
index, and skips what it does not find.

## Gestures

`[vision].enabled` runs a hand tracker over the shared camera as a second
control surface. It needs the `vision` extra and a downloaded hand-landmark
model; without either it logs one line and the show runs without it.

The default mapping mirrors the keyboard exactly:

| Gesture | While running | While paused |
|---|---|---|
| Pinch | Pause | Held, resume |
| Fast horizontal swipe | Skip | — |
| Open hand | Cycle style | — |

A frame with no hand in it is skipped rather than read as a released gesture,
and a cooldown sits on top of edge detection, because hands are noisier than key
bits.

With `[vision].performance = true`, the running-state gestures are remapped to
the clip grid: a swipe advances to the next clip, and holding a pinch or an open
hand toggles the first two effect layers. The paused-state pinch-to-resume is
unchanged in both modes, so a paused show is always recoverable the same way.

## WLED

Three independent bridges to the WLED ecosystem, pointing in three directions.
They share nothing but a configuration section, and which one you want depends
entirely on what you are trying to do.

| Direction | Setting | What it does |
|---|---|---|
| Out | `[wled].broadcast` | Real LED fixtures react to the music c64cast is playing |
| In | `[wled].listen` | The WLED app, or Home Assistant, controls c64cast |
| In | A `wled` scene | Lighting software streams pixels *to* the Commodore |

### Broadcast — Fixtures React to the Music

The music features c64cast already computes are packed into WLED's audio-sync
packets and multicast on the LAN, so strips and matrices with Sound Sync set to
"Receive" react to the SID with **no microphone** anywhere. It is pure UDP and
needs no extra.

```toml
[wled]
broadcast = "enabled"      # the multicast group: every
                           #   listener on the segment
# broadcast = "10.0.0.42"  # or one device, unicast
rate_hz = 50
```

The overall level drives brightness; each sounding voice lights the frequency
band its pitch maps to; and the transient flag most WLED effects key off is set
from an onset, or from any voice gate rising since the previous packet — sampled
at the broadcast rate, which catches essentially every note.

Only a scene with music features sends anything, so video and camera scenes go
dark. `broadcast_tempo_fallback` fills those from the beat grid instead, which
keeps the fixtures pulsing across a whole show rather than only across its SID
tunes. It is off by default, because the internal grid free-runs and would
otherwise pulse every scene unbidden.

### Listen — The App Controls c64cast

c64cast advertises itself over mDNS as a WLED device and serves a subset of
WLED's own JSON API, so the WLED mobile app, `python-wled` and Home Assistant
discover and drive it with no c64cast-specific client. It needs the `wled` extra.

The mapping is a deliberate pun on WLED's own vocabulary:

| WLED control | Reaches |
|---|---|
| Power | Pause and resume |
| Brightness | A real screen dim, all the way to black |
| Effect | The scene — the playlist is the effect list |
| Palette | The palette mode |
| Color | A forced palette of the colors you pick |
| Speed / Intensity | The current scene's live parameters |
| Presets | Save and recall the whole look |
| One segment per system | An ensemble, in order |

Brightness is deliberately **decoupled from power**: `bri = 0` dims fully to
black but does not pause. Coupling them would mean that nudging a slider through
zero resets the machine.

A self-served page at `/` mirrors the same controls in any browser, and grays out
the ones the current scene cannot use — a palette selector over a hires scene,
say, which has no palette to set. The third-party app renders a fixed control
set that cannot be disabled remotely, so a dead control there is a silent no-op.

### The Pixel Sink

The third direction is the `wled` scene of Chapter 2: LedFx, xLights, Jinx! or
another WLED device streams frames over UDP and the Commodore becomes the matrix.
Both DDP and WLED's own realtime protocol are bound at once and detected per
packet, and the frame goes through the ordinary display pipeline, so it dithers
and quantizes exactly like a camera would.

## Recording and Streaming

### The Preview Window

`[preview].enabled` opens a desktop window mirroring what the Commodore is
showing. It is a **reconstruction**, not a capture: c64cast re-renders the bytes
it sent. That is cheap and needs no capture hardware, and it has exactly the
blind spots the method implies:

- Only the modes c64cast itself draws. Sprites, raster splits, and anything a
  running program does for itself are not modeled.
- A `launcher` scene shows nothing at all — the program draws on the Commodore
  and c64cast writes no pixels.
- The staged and double-buffered bitmap paths show black, because those frames
  never pass through the host-side write path the shadow watches. Setting
  `[video].use_reu_staged = false` brings the picture back into the window.
- Text needs a real character ROM; without one it is the fallback font.

The window is pumped from the process's main thread, which is a hard requirement
of the graphics toolkit on macOS. Closing it is not a stop signal — playback
carries on headless — and a headless build with no GUI at all logs one error,
disables the window, and runs on.

**It is not visual verification.** Proving what the VIC actually put on HDMI
needs a capture device.

### Recording

`[recording].enabled` writes that same reconstruction to a video file, with the
same blind spots, needing nothing beyond the core dependencies. `path`, `fps`,
`scale` and `fourcc` are the knobs.

Across an ensemble, `enabled` in the master turns recording on everywhere, but
`path` is one of the few settings that does **not** cascade — every system needs
its own file. Leave it alone and each system records to `recording-<system>.mp4`;
set it per system and that path is used exactly as written. Pointing two systems
at one name is the one way left to lose a recording, so `--doctor` treats it as
an error.

### OBS

The integration runs one way only: the `obs_status` overlay reads the current OBS
scene and its dropped-frame count over the OBS WebSocket and paints them on the
Commodore. It needs the `obs` extra and OBS's WebSocket server switched on.
c64cast does not drive OBS.

### What a Scene Records About Itself

Every scene activation logs one `SCENE_CONFIG_JSON` line: a snapshot of that
scene's fully resolved settings — the display mode, the `[color]` and `[audio]`
knobs, the backend, the video standard, the SID model — plus a source block
carrying a video's original URL, or a tune's real name, author and release from
its PSID header.

It is designed to be pasted into a public video description, which is why two
things are deliberately absent: no connection details of any kind — no address,
no password, no serial port — and nothing resolved past the value you configured.

```bash
c64cast --config show.toml --log-file run.log
python scripts/scene_config_to_description.py run.log
```

renders the last entry as a paste-ready block, with `--all` or `--index N` for
the rest.

## Several Commodores at Once

An `[ensemble]` master file drives several machines from one process, each with
its own connection, playlist and worker thread. Chapter 1 covers the file and the
cascade; this is what the systems do to each other while they run.

### The Audio Slot

There is one room and several SIDs, so at most one system may make sound at a
time. A scene whose type inherently produces audio — video, waveform, midi,
launcher — claims the ensemble's audio slot when it starts, and a system that
cannot get it **skips to the next non-audio scene in its own playlist** rather
than waiting.

The scenes that are inherently live and quiet — a webcam, a blank canvas — are
built with no audio at all in ensemble mode, so they never compete for it.

The failure mode to design around is a system whose playlist is *entirely*
audio-bearing: it will idle whenever another system holds the slot, and the
loader warns about exactly that at load time. The one deliberate exception is a
launcher scene's `bypass_audio_lock`, which lets several interactive stations run
at once, each player hearing their own machine.

### Span and Mirror

A scene with `orchestrate = true` spans the whole wall instead of running on one
screen. That system becomes the **conductor**; every other system is interrupted
and runs a **follower** scene until the conductor releases it.

The shipped orchestrator is a **span**: each follower renders a slice of the
conductor's content, so N screens act as one canvas 320·N pixels wide. It claims
a blank or multicolor-text scene carrying a `big_text` overlay, and refuses
unless the conductor is the *rightmost* system — the message enters from the
right edge, so any other conductor is geometrically wrong. That is what makes the
`systems` order in the master file load-bearing: index 0 is the leftmost physical
screen.

A **mirror** — every screen rendering the same content in lockstep — uses the
same protocol and is not yet implemented.

A follower prefers a scene in its *own* configuration with the same `name` and
`orchestrate = false`, so per-system visual parameters take effect without
minting a second conductor; failing that it uses the conductor's own scene.
`follower_only = true` marks a scene as available for that override and excluded
from the normal rotation.

Two rules keep it honest. A scene that no orchestrator claims, or that more than
one claims, is a configuration error reported at load rather than a broken
broadcast debugged live. And a conductor that finds a broadcast already running
renders locally instead of hanging.

### One Surface for the Whole Ensemble

The control plane, the MIDI listener and the WLED device are **process-wide**,
not per-system. There is one of each for the whole wall, and each addresses
individual systems its own way:

| Surface | Addresses a system by |
|---|---|
| MIDI | The channel — channel *N* addresses system *N* in ensemble order, and `broadcast_channel` (16 by default) addresses all of them at once |
| WLED | A segment, in ensemble order |
| The web console | A tab per system |

The MIDI convention is the one worth internalising: a performer retargets by
changing their controller's transmit channel, with no round trip through a menu
or a network call. In a single-system run the channel is ignored entirely.
