# VCFSW 2026 video wall

A 3-system Commodore video wall for the **Vintage Computer Festival
Southwest**, run by the **Central Texas Commodore Users Group**. Three
Ultimate 64s in a row, each driven by `c64cast` from one host, all
looping a similar-but-staggered playlist so the screens never march in
lockstep.

```
+----------+   +----------+   +----------+
|          |   |          |   |          |
|   LEFT   |   |  MIDDLE  |   |  RIGHT   |
|          |   |          |   |          |
+----------+   +----------+   +----------+
   cam 0          cam 1          cam 2
```

## Run

```bash
python -m c64cast --config config/vcfsw-2026/master.toml
```

Each per-system file is standalone too, handy for setup/testing:

```bash
python -m c64cast --config config/vcfsw-2026/left.toml
```

## Before you run — edit these

1. **U64 addresses** — set `[ultimate64].url` in each of `left.toml`,
   `middle.toml`, `right.toml` to your three machines.
2. **Cameras** — `[video].device` is `0 / 1 / 2`. The whole ensemble
   runs in **one process on one host**, so each system needs its *own*
   capture device on that host. Run `python -m c64cast --list-devices`
   to find indices. If a system has no camera, delete its webcam scenes.
3. **Assets** — drop videos in `assets/videos/`, images in
   `assets/pictures/`, and HVSC under `assets/sids/`. The configs point
   at *directories*, never specific filenames, so you can add/swap/remove
   files anytime without touching the configs.

## What each screen does

Every screen runs the same *kinds* of scenes — live webcam, SID
oscilloscope, single-image gallery cards, big-text greetings, muted
videos, and (on the left/right) a playable launcher — but with
different content, display modes, and a different **starting scene** so
the wall stays visually varied:

| Screen | Opens on  | SID composers                         | Launcher | RSS ticker   |
|--------|-----------|---------------------------------------|----------|--------------|
| left   | webcam    | Rob Hubbard, Jeroen Tel, Tim Follin   | yes      | Hackaday     |
| middle | gallery   | Martin Galway, Matt Gray, C. Hülsbeck | no       | —            |
| right  | video| Ben Daglish, Charles Deenen           | yes      | Ars Technica |

Eight of the most celebrated SID composers are spread across the wall
(no screen repeats one), and **every** screen also carries one wildcard
"random SID" oscilloscope scene that pulls any tune from the whole HVSC
MUSICIANS tree. The waveform scenes all use `persistence = "random"` so
each trail length is picked fresh per loop.

**Videos** are muted (`audio = false`), pull a random clip from
`assets/videos/` each loop, and render only as multi-hires per-cell or
Canny edges (`mhires`/`percell` or `hires_edges`) — interleaved
throughout every screen's playlist.

**Launchers** (left + right only) hand the machine over to a random
`.prg`/`.crt` from `assets/programs/` with a 120 s idle timeout that
resets on joystick input, so a game stays up while someone is playing.

The **middle** screen runs on an Ultimate II+ (slower than a U64), so
every middle scene is capped at `target_fps = 30` to keep host load
comfortable; the left/right U64s run at the full system rate.

## Audio

- `[audio].enabled = true` (in `master.toml`) so the **waveform scenes
  play the real SID**.
- **Live webcam and video scenes are muted** (`audio = false`).
- The ensemble allows **one** audio-driving scene across the wall at a
  time. Waveform (and even muted video) scenes claim that slot; a
  system that can't get it **skips that scene** and moves on. The result
  is that the SID-playing screen takes turns around the wall and the
  music never overlaps — which is exactly the staggering you want.

## Signature live PETSCII webcam

Each screen's PETSCII webcam scene carries the three overlays requested:

- **weather** in the upper-left (open-meteo, Richardson/Dallas TX),
- **time + date** in the upper-right,
- a **retro/maker RSS ticker** scrolling along the bottom row.

## Big-text greetings

The big-text scroller cycles three messages (order and colors vary per
screen):

- `WELCOME TO VCFSW 2026!`
- `CENTRAL TEXAS COMMODORE USERS GROUP`
- `RETROCOMPUTING IS THE FUTURE`

## Live controls (at the C64 keyboards)

- **C=** (Commodore key): pause / hold 3 s to resume.
- **CTRL**: skip to the next scene.
- **SHIFT**: cycle the current scene's style (PETSCII style packs,
  big-text color, next SID subtune, etc.).
