---
number: 5
---

# Going Further

Everything so far has been about one Commodore, running by itself. This
chapter is about the ambitious end of c64cast: several machines working as
one screen, playing a playlist live like an instrument, and connecting the
Commodore to the modern lighting ecosystem. None of it is necessary. All of
it is fun.

## Driving Several Commodores at Once

One c64cast process can run any number of Commodores simultaneously, each
with its own connection, its own audio, and its own playlist. Together they
become a video wall.

The arrangement has one master file listing the systems, and one ordinary
configuration file per machine:

```toml
# master.toml
[ensemble]
systems = [
    { name = "left",   config = "left.toml"   },
    { name = "middle", config = "middle.toml" },
    { name = "right",  config = "right.toml"  },
]

[interstitial]
duration_s = 3.0
```

```bash
c64cast --config example:ensemble/master
```

Two things are worth knowing before you build one.

**The order of `systems` is meaningful.** It describes the physical left-to-
right arrangement of the screens. c64cast uses it to spread content across
the wall, so listing them out of order produces a wall that is out of order.

**Each system file works on its own.** Every entry in `systems` points at a
complete, standalone configuration, so you can debug one screen in isolation
with `--config left.toml` before asking three of them to cooperate.

Settings in the master file cascade down to the individual systems, filling
in anything they have not set for themselves. That way the interstitial
style, the color pipeline and the control settings are written once rather
than three times.

The real reward is coordination. c64cast can treat the whole wall as a
single canvas: a `big_text` message scrolls off the right edge of one screen
and onto the left edge of the next, arriving as one continuous line of
letters across three physical Commodores.

## Playing c64cast Live

A running playlist can be driven from a MIDI controller, which turns
c64cast into something closer to a performance instrument than a media
player.

```toml
[midi_control]
enabled = true
port = ""
osd = "bottom"
```

With a controller connected you can jump between scenes, cycle styles, pause
and scrub video in place, set loop points and trigger them from drum pads,
and turn knobs that adjust the color pipeline live: dithering, palette,
motion smoothing, and any parameter of the current generator or effect. The
`osd` setting puts a brief readout on the Commodore's screen as you turn
something, so you can see what you are changing without looking away.

Mapping a controller by hand means knowing both its control numbers and
c64cast's internal names. Do not do that. Run:

```bash
c64cast --midi-setup
```

and c64cast asks you to move each control in turn, learning your layout and
writing the mapping out for you.

> [!TIP]
> There is more here than one section can cover: a tempo and beat grid, a
> clip-launch grid with lit pads on controllers that support it, and a layerable
> effect chain. You can also drive the whole thing from a phone across the room —
> that is [The Browser Console](#the-browser-console), later in this chapter. The
> [Programmer's Reference Guide](https://github.com/kfox/c64cast/tree/main/docs/reference)
> documents each in full, and its
> [Performance Card](https://github.com/kfox/c64cast/tree/main/docs/card)
> is two printed pages of the same thing for the desk beside your controller.

## Controlling It From Other Things

MIDI is not the only way in.

**The Commodore's own keyboard works.** With no extra configuration, the
Commodore key pauses and resumes, <kbd>CTRL</kbd> skips to the next scene,
and <kbd>SHIFT</kbd> cycles the current scene's style. Pressing
<kbd>SPACE</kbd> opens a menu on the Commodore itself for adjusting the
running scene, which is a pleasing thing to demonstrate.

**A small web service** can be enabled with the `[control]` section,
offering endpoints to pause, resume, skip and reload. It is the practical
way to wire c64cast into a stream deck, a home automation system, or a
button by the door.

**A browser** does all of that and a great deal more; it has
[its own section](#the-browser-console) below.

**Hand gestures** work if you have a webcam and the optional `vision`
feature: pinch to pause, swipe to skip, open hand to cycle. It is exactly as
silly and as delightful as it sounds.

**A hangup signal** picks up an edited playlist without interrupting playback,
which is useful for a long-running installation you want to change in place.
Send it with `kill -HUP`, and the new scenes start at the next scene boundary.
Settings that were fixed when c64cast started — the connection, the audio path,
the camera — still need a restart.

## The Browser Console

Everything so far has assumed you are at a terminal. You do not have to be.

```bash
c64cast --serve
```

This changes what the program *is*. Instead of running one playlist and
exiting, it becomes a host: it holds the Commodore and starts, stops and
switches shows when something asks it to. That something can be your phone.

It needs the `web` feature, which `[all]` in Chapter 1 already installed. On
startup it prints a link with its own password in it:

```
web console: open http://127.0.0.1:8123/api/login?token=…
```

Open that on any device on your network — replacing `127.0.0.1` with the
computer's own address — and the password is remembered on that device from then
on. Set `[web].host = "0.0.0.0"` if you want it reachable from a phone rather
than only from the machine it runs on.

> [!WARNING]
> Anyone holding that link can start, stop and edit shows, which means running
> whatever those files describe. Treat it as the password it is. To let somebody
> just *watch*, use the read-only link described at the end of this section.

### The Three Screens

**Session** is the host itself: which configuration is loaded, what the
Commodore is doing, every playlist the host can see, and buttons to start,
switch, reload and stop. Point it at your playlists once —

```toml
[web]
config_roots = ["~/shows"]
```

— and it lists everything in there. Leave `config_roots` empty and it uses
wherever you launched the host from.

**Configs** is an editor, and this is the part that saves the most typing. Pick
a file and you get a form: every setting the file changes, with the same
one-line explanation `--describe` prints, a control suited to its type — a
switch, a picker of exactly the values it accepts, a colour field that draws the
sixteen C64 colours as swatches — and a `live` mark on the ones a running show
would pick up without restarting. Nothing is written until you press **Save**,
and a save that would produce a file that will not run is refused, with the
reason, and the file untouched.

You can add scenes here too: **Add scene** makes a blank one of the type you
pick, and **Duplicate** copies a scene you have already tuned, which is the
quick way to say "another clip like that one". There is still a *Source* tab
with the raw TOML for the few things a form cannot do — changing a scene's
`type`, editing an overlay, or keeping the comments you wrote by hand.

**Live** is the screen to have open while a show is playing. At the top is the
Commodore's own picture, in the browser. It is not c64cast's idea of the frame:
the Ultimate 64's FPGA taps the VIC's output directly and sends it over the
network, taking no C64 cycles, so it is what the machine actually painted —
right even for a game running under the launcher. Press **Watch** to start it
and **Stop** when you are done, because it is a couple of megabytes a second
while it runs.

> [!NOTE]
> The live picture is an Ultimate 64 feature. An Ultimate II+ is a cartridge in
> somebody else's C64 and has no VIC of its own to tap; a TeensyROM+ has no
> video path at all. Both say so in the panel rather than showing you a blank
> box. Everything else on this screen works on all three.

Below it: the tempo with a **Tap** button, pause, resume and skip, the clip
grid, the effect rack, the playlist with a tap to jump straight to any scene,
and **Tune** — the colour pipeline, the generator's knobs, a scope's gain. Tune
shows what the *current scene* actually has, so it changes shape as the show
advances and never offers you a slider that does nothing.

### Keeping What You Tuned

A run started from a terminal asks "save these changes?" as it exits. A host has
no terminal to ask on, so it asks here instead: under the Tune knobs is a record
of every colour change since the show started, where it began and where it is
now, and one tap writes them into the configuration the show is running from.
**Discard** drops the offer and leaves the show alone.

It is a patch of the file rather than a rewrite, so anything else you edited in
the meantime survives. A change with nowhere to go — a palette tuned on a clip
your file does not contain — is listed as *runtime only* rather than written
somewhere it does not belong.

### Sharing It

To let somebody watch without handing over control, open **Session** and ask for
a read-only link. It follows the show and can do nothing else: no start, no
stop, no tuning, no edits. Hand that one out.

### If the Console Will Not Load

The console is built and shipped inside c64cast, so there is normally nothing to
install. If you are running from a checkout of the source rather than an
installed copy, the bundle may never have been built — the host says so at
startup and serves the older `/perf` page instead, which has no dependencies of
its own and covers the whole performance surface. `make web` builds the full
console, and that is the only part of c64cast that needs Node.

The [Programmer's Reference Guide](https://github.com/kfox/c64cast/blob/main/docs/reference/07-inputs-and-outputs.md)
documents every route and every panel in full.

## The LED Bridge

c64cast connects to the WLED lighting ecosystem in three separate
directions, all configured in one section:

```toml
[wled]
broadcast = "disabled"
listen = "disabled"
name = "c64cast"
```

**Broadcasting out.** Turn on `broadcast` and c64cast drives real LED
strips and matrices from the music the Commodore is playing. It takes the
level, per-voice notes and onsets straight from the SID and sends them to
your lights. Note what is absent: no microphone. The lighting is driven from
the actual synthesizer state rather than from a listening device, so it is
perfectly in time.

**Listening in.** Turn on `listen` and c64cast announces itself on the
network as a WLED device. The WLED phone app finds it, and Home Assistant
finds it, and from there the app's effects select your scenes and its
sliders adjust your live parameters. Controlling a Commodore 64 from a
lighting app that has no idea what it is talking to is a fine joke that
also happens to be useful.

**Receiving pixels.** The `wled` scene from Chapter 3 makes the Commodore
itself the LED matrix, receiving a live pixel stream from LedFx or xLights.

## Recording What You Made

Every time a scene starts, c64cast writes a complete description of it to
the log: the display mode, the color settings, the hardware, and where the
material came from, including a SID tune's real name and author from its
file header.

The record deliberately excludes your Commodore's address and any password,
because it is designed to be published. Run with `--log-file` to keep it:

```bash
c64cast --config my-playlist.toml --log-file run.log
```

Each entry is one line of JSON, so anything that reads JSON can turn it into a
video description. The repository carries a
[small script](https://github.com/kfox/c64cast/blob/main/scripts/scene_config_to_description.py)
that renders one as a pasteable block, if you would rather not write your own.

## Where To Go Next

You have reached the end of the guided part. From here:

- [**The Programmer's Reference Guide**](https://github.com/kfox/c64cast/tree/main/docs/reference)
  is the second volume: every scene, every knob and every register, with nine
  appendices generated from the code. It is the book to open when you know what
  you want and need to know exactly what it is called.
- **`c64cast --list-examples`** lists the runnable demonstration of
  every scene and every overlay that ships inside c64cast (browsable
  [on GitHub](https://github.com/kfox/c64cast/tree/main/c64cast/examples)).
- [**`docs/architecture.md`**](https://github.com/kfox/c64cast/blob/main/docs/architecture.md)
  explains how it all works inside, and why certain things are the way they
  are.
- [**`docs/caveats.md`**](https://github.com/kfox/c64cast/blob/main/docs/caveats.md)
  is the collected hard-won knowledge about the hardware's sharper edges.

Thank you for reading. Go and put something strange on a Commodore.
