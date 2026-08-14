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
> clip-launch grid with lit pads on controllers that support it, a layerable
> effect chain, and a web console you can open on a phone to drive the whole
> thing from across the room. The
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

**A host that stays up** is the larger version of that idea. `c64cast --serve`
turns the program into a server that holds the Commodore and starts and stops
shows on request, rather than running one playlist and exiting — handy for a
machine in the corner of a room you would rather drive from a phone than from a
terminal. It prints a link with its own password in it when it starts; opening
that link gives you a page showing what the Commodore is doing, the playlists
the host can see, and buttons to start, switch and stop them. Point
`[web].config_roots` at the folder your playlists live in and it will list them,
edit them, and start whichever one you pick — with the caveat that anyone
holding that password can then run whatever those files describe.

The page is part of c64cast, so there is nothing to install and no separate
service to run. It updates itself as the show changes, which means it also
follows a playlist someone else started from a controller across the room.

**Hand gestures** work if you have a webcam and the optional `vision`
feature: pinch to pause, swipe to skip, open hand to cycle. It is exactly as
silly and as delightful as it sounds.

**A hangup signal** picks up an edited playlist without interrupting playback,
which is useful for a long-running installation you want to change in place.
Send it with `kill -HUP`, and the new scenes start at the next scene boundary.
Settings that were fixed when c64cast started — the connection, the audio path,
the camera — still need a restart.

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
