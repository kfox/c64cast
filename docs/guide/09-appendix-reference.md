---
number: A
---

# Quick Reference

A place to look things up once you know what you are looking for.

## Asking c64cast What It Can Do

These commands need no hardware, no configuration file and no network. They
answer from c64cast's own definitions, so they are always current for the
version you have installed.

| Command | Answers |
|---|---|
| `--compat` | The overlay against display-mode matrix |
| `--describe NAME` | Everything about one scene, overlay, section or mode |
| `--list-dac-calibrations` | The DAC calibrations measured on this computer, by the name to play one with |
| `--list-devices` | Cameras and audio devices this computer can see |
| `--list-modes` | Every display mode |
| `--list-overlays` | Every overlay, and which modes it works on |
| `--list-scenes` | Every kind of scene, with a one-line description |
| `--print-schema` | The configuration schema, as JSON |
| `--suggest-palette FILE` | The C64 colors that best represent an image |

`--describe` takes a bare name when it is unambiguous, and a prefixed one
when it is not:

```bash
c64cast --describe clock
c64cast --describe scene:video
c64cast --describe section:color
c64cast --describe mode:mhires
```

## The Options You Will Actually Use

`c64cast -h` prints every option, grouped. The ones worth
remembering:

| Option | Does |
|---|---|
| `--check-for-updates` | Ask PyPI whether a newer c64cast release exists |
| `--config PATH` | Use a specific configuration file |
| `-D INDEX`, `-D NAME` | Choose the audio input |
| `-d INDEX`, `-d NAME`, `-d VID:PID` | Choose the webcam |
| `--doctor` | Check everything and report |
| `--dump-char-rom` | Re-read the character ROM from the Commodore |
| `--init` | Build a configuration interactively |
| `--install-char-rom PATH` | Use a character ROM dump you already have |
| `--log-file PATH` | Also write the log to a file |
| `--loop` / `--no-loop` | Repeat the playlist, or stop after one pass |
| `--no-audio` | Mute |
| `-s NTSC` or `-s PAL` | Set the machine's timing |
| `--save-settings` | Remember this run's connection and devices |
| `--serve` | Run as a host with a browser console instead of playing once |
| `--skip-probe` | Run checks without touching the Commodore |
| `-u TARGET` | Choose the hardware and where to find it |
| `--upgrade` | Detect how you installed c64cast and upgrade it, one command either way |
| `-v` | Debug logging (`-vv` adds a line per HTTP request) |

### Naming Devices Instead of Counting Them

`-d` and `-D` both take more than a bare index. Device numbers are a poor
thing to rely on — they shuffle when you plug something in — so prefer to name
the device instead. Both options match on **any part of the device's name**,
case-insensitively, and `-d` will additionally take a USB `VID:PID` when two
devices share a name.

Start by asking what is attached:

```bash
c64cast --list-devices
```

Then use enough of the name to be unambiguous:

```bash
c64cast -d "HD Webcam" -D "Scarlett" clip.mp4
```

Name matching for cameras needs the `camera` extra, and for audio the `mic`
extra; the `[all]` install in Chapter 1 has already provided both.

Best of all, do it once:

```bash
c64cast -d "HD Webcam" -D "Scarlett" --save-settings
```

Now every future run picks the right devices by itself, and you can stop
passing either option at all.

## Where Settings Come From

When the same setting is given in more than one place, the later one wins:

1. c64cast's built-in defaults.
2. Your machine settings, from `--save-settings`.
3. The configuration file.
4. Options typed on the command line.
5. The `C64CAST_DMA_PASSWORD` environment variable, for the password only.

## Where Files Live

| What | Where |
|---|---|
| Machine settings | `~/.config/c64cast/settings.toml` |
| Saved data, calibrations, presets | `~/.local/share/c64cast/` |
| The configuration c64cast finds by itself | `./c64cast.toml` |

Both locations follow the usual conventions for your operating system, and
both can be redirected with an environment variable. `--doctor` prints the
paths it actually resolved, which is the quickest way to settle any doubt.

## The Configuration Sections

| Section | Governs |
|---|---|
| `[audio]` | Audio on or off, backend, sample rate |
| `[audio_features]` | Reacting to live audio input |
| `[color]` | Dithering, palette, color matching |
| `[control]` | The web control service |
| `[debug]` | Diagnostics and profiling |
| `[dsp]` | Signal shaping before audio reaches the Commodore |
| `[ensemble]` | Several Commodores driven together |
| `[hardware]` | Which backend, and the machine's timing |
| `[interstitial]` | The card shown between scenes |
| `[menu]` | The on-screen menu on the Commodore |
| `[midi_control]` | Live control from a MIDI device |
| `[performance]` | The clip grid, the beat grid, and saved looks |
| `[playlist]` | Looping, and video interleaving |
| `[preview]` | The local mirror window |
| `[recording]` | Recording to a video file |
| `[[scenes]]` | One per scene, in playing order |
| `[[scenes.overlays]]` | One per overlay, within a scene |
| `[teensyrom]` | The TeensyROM's transport and port |
| `[ultimate64]` | The C64U's address and password |
| `[video]` | Camera selection and default display mode |
| `[vision]` | Hand-gesture control |
| `[web]` | The browser console host (`--serve`) |
| `[wled]` | The LED bridge |

Run `--describe section:NAME` for the full contents of any of them.
