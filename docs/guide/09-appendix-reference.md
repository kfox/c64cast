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
| `--list-scenes` | Every kind of scene, with a one-line description |
| `--list-overlays` | Every overlay, and which modes it works on |
| `--list-modes` | Every display mode |
| `--describe NAME` | Everything about one scene, overlay, section or mode |
| `--compat` | The overlay against display-mode matrix |
| `--print-schema` | The configuration schema, as JSON |
| `--suggest-palette FILE` | The C64 colours that best represent an image |
| `--list-devices` | Cameras and audio devices this computer can see |

`--describe` takes a bare name when it is unambiguous, and a prefixed one
when it is not:

```bash
python -m c64cast --describe clock
python -m c64cast --describe scene:video
python -m c64cast --describe section:color
python -m c64cast --describe mode:mhires
```

## The Options You Will Actually Use

`python -m c64cast -h` prints every option, grouped. The ones worth
remembering:

| Option | Does |
|---|---|
| `--config PATH` | Use a specific configuration file |
| `-u TARGET` | Choose the hardware and where to find it |
| `-s NTSC` or `-s PAL` | Set the machine's timing |
| `-d INDEX`, `-d NAME`, `-d VID:PID` | Choose the webcam |
| `-D INDEX`, `-D NAME` | Choose the audio input |
| `--no-audio` | Mute |
| `--loop` / `--no-loop` | Repeat the playlist, or stop after one pass |
| `--doctor` | Check everything and report |
| `--skip-probe` | Run checks without touching the Commodore |
| `--init` | Build a configuration interactively |
| `--save-settings` | Remember this run's connection and devices |
| `-v` / `-vv` | More logging, then a great deal more |
| `--log-file PATH` | Also write the log to a file |

### Naming Devices Instead of Counting Them

`-d` and `-D` both take more than a bare index. Device numbers are a poor
thing to rely on — they shuffle when you plug something in — so prefer to name
the device instead. Both options match on **any part of the device's name**,
case-insensitively, and `-d` will additionally take a USB `VID:PID` when two
devices share a name.

Start by asking what is attached:

```bash
python -m c64cast --list-devices
```

Then use enough of the name to be unambiguous:

```bash
python -m c64cast -d "HD Webcam" -D "Scarlett" clip.mp4
```

Name matching for cameras needs the `camera` extra, and for audio the `mic`
extra; the `uv sync --all-extras` in Chapter 1 has already installed both.

Best of all, do it once:

```bash
python -m c64cast -d "HD Webcam" -D "Scarlett" --save-settings
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
| `[hardware]` | Which backend, and the machine's timing |
| `[ultimate64]` | The C64U's address and password |
| `[teensyrom]` | The TeensyROM's transport and port |
| `[video]` | Camera selection and default display mode |
| `[audio]` | Audio on or off, backend, sample rate |
| `[dsp]` | Signal shaping before audio reaches the Commodore |
| `[color]` | Dithering, palette, colour matching |
| `[interstitial]` | The card shown between scenes |
| `[playlist]` | Looping, and video interleaving |
| `[preview]` | The local mirror window |
| `[recording]` | Recording to a video file |
| `[control]` | The web control service |
| `[midi_control]` | Live control from a MIDI device |
| `[menu]` | The on-screen menu on the Commodore |
| `[vision]` | Hand-gesture control |
| `[wled]` | The LED bridge |
| `[[scenes]]` | One per scene, in playing order |
| `[[scenes.overlays]]` | One per overlay, within a scene |
| `[ensemble]` | Several Commodores driven together |

Run `--describe section:NAME` for the full contents of any of them.
