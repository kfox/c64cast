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
| No configuration file — quick playback | There is nothing to write to, so a pasteable block is printed instead |

It runs after a normal exit and after an interrupt at the terminal alike, and
in an ensemble each system is offered separately, tagged with its name.

Every parameter that has a configuration field to land in is written, and each
goes to the section that owns it. Seven do: `dither_strength`, `dither_method`,
`color_match`, `cell_strategy`, `cell_pick`, `motion_smoothing` and
`auto_fit_strength` are `[color]`, which the whole show shares; `palette_mode`
belongs to one scene, so it is written into the `[[scenes]]` block of the scene
that was playing when you turned it — turn it during two scenes and both are
kept, separately. A generator's `speed`, an effect's `decay` and the rest of the
runtime knobs are not configuration at all, so none of them is offered back.

A palette mode turned on a scene the file does not contain — a launched clip, or
a video the playlist inserted between scenes — has no block to be written into.
It is still listed, so you know it will not survive the show.

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
everything the page does from then on is authenticated. Prefer the environment
variable to a value in a file you might commit or share.

The token defaults to empty, which means open. On loopback that is allowed and
unremarkable. Off loopback it is refused: a `host` that is not `127.0.0.1`,
`localhost` or `::1` with no token set is a configuration error, reported
before the run opens the hardware and by `c64cast --doctor`. If the network is
one you trust and you want the port open anyway, say so:

```toml
[control]
allow_unauthenticated = true
```

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
process, no app to install. It shows the clip grid, an effect rack, the tune
knobs the current scene has, the record of what you have already turned, the
tempo with a tap button, pause and skip, the saved looks and a jump to any scene
of the show — one panel per system.

It drives the same engine the MIDI surface does, so a launch from a phone and a
launch from a pad are indistinguishable downstream. A touch sends press on
contact and release on lift, so momentary clips work by touch exactly as they do
from a pad, with no per-launch-type handling in the page.

The effect rack's rows are generated from each live layer's own declared
parameters, and the tune panel from the same catalog `--midi-setup` offers,
filtered to what the scene on screen actually has — so neither can drift from
what is loaded, and neither shows a control that writes nowhere. State is pushed
over a WebSocket a few times a second, and the beat indicator is extrapolated on
the page between pushes, so it animates smoothly without a round trip per beat.

Under the tune knobs is the record of turning them, and a **Keep** that writes
them into the configuration the show is running from — the same offer a run
made from a terminal makes on its way out. A run that has a terminal makes it
there instead, and says so if you tap.

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
| `POST /api/session/start` | Build and run a configuration — the launched one, or the `config` named in the body |
| `POST /api/session/stop` | Bring the running show down and put the machine back |
| `POST /api/session/switch` | Stop, wait for the hardware, and start again — re-reading the file |
| `POST /api/session/reload` | The same reload the control plane offers |
| `POST /api/session/live-tune` | Keep (or drop) the knob changes made since the show started |
| `GET /api/introspect` | Every configuration section, scene type, overlay, display mode and live target, as JSON |
| `GET /api/screen` | Which systems can show a picture, without starting anything |
| `GET /api/screen.png` | One still frame of the machine's actual screen |
| `GET /api/screen/stream` | The screen as a live `multipart/x-mixed-replace` stream, which one `<img>` renders with no script |
| `POST /api/viewer-link` | Mint (or return) the read-only login link to hand somebody |
| `GET /api/library` | Favorites and recently-launched configurations |
| `POST /api/library/favorites` | Star or unstar a configuration |
| `GET /api/configs` | The configurations the host can see, and the roots they live under |
| `POST /api/configs` | Create a new configuration — a copy of another readable one, or a minimal starter |
| `GET /api/configs/{path}` | One configuration: its text, its settings with a "left at the default" flag on each, and any stray keys |
| `PUT /api/configs/{path}` | Replace it — validated first, and the previous text kept |
| `PATCH /api/configs/{path}` | Change named settings and let the host compose and write the file |
| `DELETE /api/configs/{path}` | Delete it — refused for a read-only root or the config currently running |
| `POST /api/configs/{path}/validate` | Check text without saving it — or, with no text, check the file as it stands on disk |
| `POST /api/configs/{path}/scenes` | Add a scene — blank, or a copy of an existing one |
| `DELETE /api/configs/{path}/scenes/{index}` | Remove a scene — refused for the last one |
| `PATCH /api/configs/{path}/scenes/{index}` | Reorder a scene |
| `GET /api/media` | Media a `file =` field could name — browsable by kind, with an optional search |
| `PUT /api/media/{name}` | Upload a file, streamed straight to disk |
| `WS /api/ws` | Live state: the performance payload, the session state, and new log lines as they happen |
| `GET /` | The console itself — the browser interface to all of the above |

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

### The Console

Opening the host's address in a browser gets the console. **Session** is which
configuration is loaded, what the machine is doing, the list of configurations
it can see, buttons to start, switch, reload and stop, and the host's log as it
happens. The state arrives over the WebSocket rather than by polling, so the
page follows a show being started from somewhere else — another browser, a MIDI
controller, `curl` — without being told.

**Configs** is the editor. Pick a file and it comes up two ways. *Settings* is
the generated view: every scene and every setting the file changes, each with
the same one-line explanation `--describe` prints, what it may be set to, and a
`live` mark on the ones a running show would pick up without a restart. The
values shown are what the loader actually resolved, so a machine setting or a
default that a file never mentions still shows through — untick *only what this
file changes* to see all of it, or type a name into *Find a setting* to go
straight to one whatever the filter says.

Every row is editable. A setting gets the control its type asks for — a switch,
a picker of exactly the values it accepts, a number, a box of JSON for a list or
a table — and an edited row is marked, counted, and saved in one go by **Save**;
**Undo** drops one edit and **Discard** drops them all. **Clear** is the other
direction: it stops the file setting a field at all, and the row shows what will
apply instead. Nothing is written until you save, and a save that would produce
a file that cannot run is refused with the loader's own reason, the file
untouched and the edits still on screen.

A setting that accepts two kinds of value offers both, with a small selector
above the control saying which you are writing. A color is the case that
matters: `border` and `background` take a name *or* an index `0..15`, so they
get the sixteen C64 colors as swatches, and `force_palette_colors` takes either
a count or a list of them. Picking a swatch writes the color's name. A spelling
the picker cannot place — the short forms the loader also accepts, like `lgrn` —
is left exactly as it is, and said to be unrecognized, rather than quietly
changed to something else.

Scenes can be added and removed here too. **Add scene** under the list makes a
blank one of the type you pick; **Duplicate** on a scene makes a copy of it
right after it, which is the quick way to add another clip like the one you
already tuned; **Remove** drops one, except the last, since a show needs a scene
to play. Those write the file straight away, so save or discard your staged
edits first — the console says so, because inserting a scene renumbers the ones
after it. Changing a scene's `type` and editing an overlay are still the
*Source* editor's: each rewrites the block rather than setting a value in it.

**Check** and **Save** also warn about media. A scene naming a file that is not
on this host still loads — the file may arrive before showtime, or belong to
another machine in an ensemble — so it is a warning and not a refusal, but it is
said before the C64 is opened rather than seconds into the run.

*Source* is the file itself, editable, with **Check** to load it without saving
and **Save** to write it back. An edit you have not saved — in either view —
survives clicking away to another file, and survives leaving the Configs screen
entirely; it is marked in the file list and on the Configs tab, so nothing is
lost by looking at something else. A configuration is addressed by its own URL —
`/config/shows/gig.toml` — which makes it a link worth sending. When the file
on screen is the one the session is running, the screen says so and offers
**Reload scenes**, because saving to disk and putting it on the C64 are two
different acts.

A reload is not always enough, and the console says which of your changes it
covers. A reload re-reads the file and rebuilds the scenes; `[audio]`, `[video]`
and `[ultimate64]` are read once when the session starts and their threads are
already running, so a change to one of those needs the session restarted. The
save message splits its own count that way, the unsaved-changes line warns
before you save rather than after, and when a reload would leave something out
the banner offers **Restart on this config** beside it.

If a save is refused for something you cannot find in the file, look at what the
refusal names: a configuration is checked with your machine settings underneath
it, and when the offending value comes from there rather than from the file on
screen, the console says so and names the file it came from.

> [!NOTE]
> The form saves the file, not the machine. A value that comes from your machine
> settings (`~/.config/c64cast/settings.toml`) shows in the form as the resolved
> value but is *not* written into the show file — set it in the form and it is
> written, **Clear** it and the machine setting shows through again. That keeps
> a show file portable: it says what the show is, not what this machine is.

**Live** is the performance surface, and it is the screen to have open at a
gig. Above everything else is the **Screen** panel: the Commodore's picture,
live, in the browser. It is the machine's own video — the Ultimate 64's FPGA
taps the VIC and sends it out as UDP, without taking a single C64 cycle — so
what you see is what the VIC is actually painting rather than what c64cast
believes it wrote, and it is right for scenes c64cast does not draw at all.
Press **Watch** to start it and **Stop** to end it; it runs only while the
panel is open, because it is a couple of megabytes a second while it does.

This is the one feature the hardware decides. An Ultimate 64 has it. An
Ultimate II+ is a cartridge in someone else's C64 and has no VIC of its own to
tap; a TeensyROM+ has no video path at all. Those say so in the panel rather
than showing you nothing. `[web].screen_fps` sets how often the host encodes a
frame (not how fast the machine sends), and `0` turns the screen off.

Along the top is the beat grid: the tempo, a pulse on the current beat,
where that tempo came from, **Tap** to set it by hand, and the transport —
**Pause**, **Resume** and **Skip**, which are the same pause and skip the C64's
own keys give you. Below it the clip grid, one pad per `[[performance.clips]]`
entry, lit green for the clip playing and amber for one waiting on its quantize
boundary — with a count-in beside the tempo saying how many beats are left. Pads
are pressed and released rather than clicked, so a `gate` clip holds for as long
as your finger is down.

The **effect rack** lists the current scene's chain with a bypass button and a
slider per knob, generated from what each effect declares, so it cannot offer
one the effect does not have. **Tune** is the rest of the live surface: the
color pipeline (dither strength and method, palette mode, color matching, cell
strategy, motion smoothing, auto-fit), the generator's own knobs, and a scope
scene's gain — the same knobs `--midi-setup` offers a controller, grouped the
same way. It shows what the *current scene* has and nothing else, so it changes
shape as the show advances and never offers a slider that does nothing. A
color-pipeline change made here is recorded like any other live tune, and the
record sits under the knobs: every change since the show started, where it began
and where it is now. One tap keeps them in the file the show is running from —
a patch of that file rather than a rewrite of it, so anything else you have
edited there survives — and **Discard** drops the offer without touching what is
playing. A run started from the command line asks the same question when it
exits; the host has no terminal to ask on, so it asks here instead, at a moment
you choose rather than at a shutdown. Each change goes to the part of the file
that owns it: the shared `[color]` settings, or — for a palette mode, which
belongs to a scene — that scene's own block, tagged in the list with the scene
it will be written to. A change with nowhere to go, such as a palette mode
turned on a launched clip the file does not contain, is marked *runtime only*
and ends with the show; and a quick-playback run, which has no file to write to,
gets a block to paste into one.

**Scenes** lists the playlist with the one playing marked; tapping one jumps
straight to it, without the interstitial in front. The eight look pads recall a
saved look; arm **SAVE** first and a pad stores the current clip and effect chain
instead. Everything here drives the same engine a MIDI controller drives — a
pad tapped in the browser and a pad tapped on the grid are the same launch —
and an ensemble puts each machine on its own tab, at its own URL.

The host's **log** sits in a bar along the bottom of every screen, showing the
last line and opening in place, so a refused save or a scene that failed is
readable from wherever you were standing when it happened.

It is built and **shipped inside the package**, so there is nothing to install
and no build step: `uv sync` and `pip install` both give you a console. It is
also gated exactly like the rest of the surface, which means the first thing an
unvisited browser sees is a box asking for the token — paste the one the host
printed and it is remembered, on that browser, until you clear its cookies.

If you are working on the console's own source, that lives in `web/` in the
repository and `make web` rebuilds it; only that needs Node.

The `/perf` performance console is still there, on the same host, and still has
no dependencies of its own. It is the older page but not the lesser one: it
carries the same panels this console does — tune, the record and its **Keep**,
scenes with a jump, transport beside the tap tempo — so it is the one to reach
for on a gig day when the bundle is not there. If a bundle was never built — a
checkout that has not run `make web` — the host says so at startup and serves
`/perf` instead.

### Browsing And Editing Configurations

`config_roots` lists the directories the console may read and write `.toml`
files in. Leave it empty and it is wherever the host was launched from:

```toml
[web]
config_roots = ["~/shows", "~/experiments"]
```

Files are named by root rather than by path — `shows/gig.toml`, not
`/home/you/shows/gig.toml` — and the root's label is its own directory name.
Nothing outside a root is readable or writable, including through a symbolic
link planted inside one, and nothing but `.toml` is addressable at all. Those
same names are what `start` accepts:

```bash
curl -X POST -H "X-C64Cast-Token: $TOK" \
     -d '{"config": "shows/gig.toml"}' \
     http://127.0.0.1:8123/api/session/start
```

A save is validated before it lands: text that does not load is refused with
`422` and the file is untouched. What was there is copied to a hidden sibling
(`.gig.toml.bak`) first, which is the only undo there is. Reading a
configuration returns its raw text *and* a per-field view carrying each value,
what it falls back to when the file stops naming it, and whether the two agree —
which is what the console's form uses to show only what you set, and to say what
**Clear** will leave behind. `PATCH` takes named field edits rather than text
and lets the host compose the file through the same dataclasses the loader uses,
so the browser never writes TOML and two consoles editing different settings do
not overwrite each other. Ensemble
master files read but have no such view — they are authored across several files
— so they are edited as text.

> [!WARNING]
> A configuration you can save names media paths and URLs that a show will then
> open, and a video source can be an address on the internet. Being able to
> write configurations remotely is therefore close to being able to run things
> on the host: the root list bounds *which files are edited*, not what a saved
> file can reach. Keep the full token private, and hand out `viewer_token` to
> anyone who only needs to watch.

### Browsing And Uploading Media

`media_read_write` maps each media kind to the directory the Editor's
`file =` fields both browse for existing media *and* upload new media into —
a datalist of what is actually there, instead of a bare text box you have to
fill from memory, plus a drop zone and an **Upload…** button on the field
itself. Leave it empty and it is the four directories the loader itself
already defaults to (`assets/videos`, `assets/sids`, `assets/pictures`,
`assets/programs`); naming a kind only ever changes that one kind, so setting
one to `""` turns its uploads off without disturbing the rest:

```toml
[web]
media_read_write = { video = "assets/videos", sid = "" }
media_read_only = ["~/Movies", "/mnt/hvsc"]
```

`media_read_only` adds directories that are browsable but never a destination
— a library you want offered without exposing it to uploads. Unlike
`config_roots`, every kind is browsed across the same combined list at once —
a scene's own type decides which kind (video, `.sid`, still image, program, or
generative audio) it offers, not which directory is searched. A directory
inside a root shows up too, whenever it directly holds a matching file, since
`file =` already treats a directory as a random pick per scene.

An upload never overwrites anything already there: a name already taken is
renamed `clip-2.mp4`, `clip-3.mp4`, and so on. `audio` has no default
directory (`generative`'s `audio_source = "file"` is the one scene type that
requires an explicit `file =`), so name one to allow audio uploads at all.

### The Token Is Not Optional Here

Unlike the control plane, this surface has no unauthenticated mode. It starts
and stops hardware, so if no token is configured one is generated, stored
`0600` under the data directory, and printed at startup as a URL you can open:

```
web console: open
  http://127.0.0.1:8123/api/login?token=…&next=/
```

Opening it trades the token for a cookie and lands on the console. A browser
that arrives without one gets a form to paste it into instead.

Set `[web].token` (or `$C64CAST_WEB_TOKEN`, which wins) to choose your own, or
`token_file` to keep it out of the configuration entirely. `viewer_token` grants
the same read-only role the control plane's does: watch the state feed, but
never start, stop or edit.

You do not have to configure that one to use it. The Session screen's **Share**
block asks the host for a read-only link and shows it ready to copy; the first
ask mints the token and keeps it, so the link still opens after a restart. Hand
that out rather than the address you are using — yours can stop the show.

### Living Through a Crash

A host writes a small marker file while a show runs and removes it on the way
down. If it finds one at the next start, the previous run died with the machine
still mid-show — so before building anything it opens a bare connection, resets
the Commodore, and closes it again. That makes a host under `launchd` or
`systemd` strictly safer than the one-shot command, which has no second chance
at that reset.

A preview window under `--serve` works from a terminal but is not a supported
way to run one. What the console offers instead is the **Screen** panel above —
which is a better answer on an Ultimate 64, being the VIC's own output rather
than the host's idea of it, and no answer at all on the other two backends,
which have no video to tap. A preview of what the *render path* produced, which
is what `[preview]` shows locally and what would work on every backend, is still
to come in the console.

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

One line is yours to finish. A video scene's `copyright` reads `unknown` —
c64cast records what it played, never what you are allowed to publish, and it
would rather say so than guess. A tune is the exception: a PSID header usually
names its own author and year, and those are reported as written.

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
