# Figure shot list

How each figure in [`../img/`](../img/) was actually captured, so any of them
can be reshot without rediscovering the setup. The `.toml` files here are the
configs the captures were made with; figures that need no config of their own
name the command instead.

These configs deliberately carry **no `[ultimate64]` section**. Connection
comes from your machine settings (`~/.config/c64cast/settings.toml`) or an
explicit `-u`, which keeps addresses out of the repo and makes the configs work
on someone else's bench.

They do reference things under `assets/`, which is git-ignored apart from its
READMEs — so the media is not in the checkout. Substitute your own; the notes
in each config say what a given figure needs from its source material.

## Before you shoot anything

The U64 ships with **HDMI Scan Resolution** at SD (480p/576p), and the capture
device then offers only 640×480 — far too soft for print. Scanlines are on by
default too, and at 4.5 px per raster line they alias badly once a figure is
scaled onto a page. Set both, and put them back afterwards:

```bash
uv run python scripts/capture_guide_figure.py hdmi --capture
# ... shoot ...
uv run python scripts/capture_guide_figure.py hdmi --restore
```

Both settings are volatile — the firmware only persists on an explicit save —
but restore anyway rather than leave a machine reconfigured.

## The workflow

[`scripts/capture_guide_figure.py`](../../../scripts/capture_guide_figure.py)
runs a config, grabs frames off the capture device, crops the HDMI pillarbox
down to the C64 frame, and stops the app cleanly. Sample a run, review the
candidates as one contact sheet, then install the keeper:

```bash
uv run python scripts/capture_guide_figure.py shoot \
    --config docs/guide/shots/fig-3-1-waveform.toml --label wave --at 14 -n 15 --spacing 2
uv run python scripts/capture_guide_figure.py sheet wave
uv run python scripts/capture_guide_figure.py install wave_05 fig-3-1-waveform
```

Frames land in `scripts/diags/out/guide/` (git-ignored).

## The figures

| Figure | Captured from | Notes |
|---|---|---|
| `fig-qs-1-hello.png` | `c64cast/examples/hello.toml` | `--burst 110 --at 36.6`, then `center hb` to pick the frame where HELLO is actually centered |
| `fig-qs-2-video.png` | [`fig-qs-2-video.toml`](fig-qs-2-video.toml) | `--at 10 -n 14 --spacing 0.7` |
| `fig-ft-1-slideshow.png` | `c64cast assets/pictures/` | quick playback; `--at 9 -n 18 --spacing 2.5` and pick |
| `fig-1-1-doctor.png` | `c64cast --doctor --skip-probe` | terminal; see below |
| `fig-2-1-interstitial.png` | [`fig-2-1-interstitial.toml`](fig-2-1-interstitial.toml) | `--at 10 -n 24 --spacing 1.4` |
| `fig-2-2-wizard.png` | `c64cast --init` | terminal, driven by keystrokes; see below |
| `fig-3-1-waveform.png` | [`fig-3-1-waveform.toml`](fig-3-1-waveform.toml) | `--at 14 -n 15 --spacing 2` |
| `fig-3-2-generative.png` | `c64cast/examples/scene-generative-plasma.toml` | `--at 12 -n 10 --spacing 1.8` |
| `fig-3-3-webcam.png` | [`fig-3-3-webcam.toml`](fig-3-3-webcam.toml) | check `[video].device` first — see the config |
| `fig-4-1-modes.png` | [`fig-4-1-modes-*.toml`](.) ×4 | four runs, then `plate`; see below |
| `fig-4-2-overlays.png` | [`fig-4-2-overlays.toml`](fig-4-2-overlays.toml) | `--at 14 -n 12 --spacing 1.5` |

### The four-mode plate

One run per mode, then compose the panels into a labeled grid:

```bash
for m in petscii mcm hires mhires; do
  uv run python scripts/capture_guide_figure.py shoot \
      --config docs/guide/shots/fig-4-1-modes-$m.toml --label p-$m --at 16 -n 1
done
uv run python scripts/capture_guide_figure.py plate fig-4-1-modes \
    p-petscii_00=petscii p-mcm_00=mcm p-hires_00=hires p-mhires_00=mhires
```

`plate` also takes `--cols`, and a trailing `=` on a panel means "no label" —
for a plate whose panels should not be named.

### The ensemble wall

Chapter 5 has no figure. A wall shot has to show three screens at the *same
instant*, and one capture device can only watch one screen, so there is no
straightforward way to photograph one. Working around that is a problem to
solve when the figure is actually wanted, not before.

### The terminal figures

[`scripts/capture_terminal_figure.py`](../../../scripts/capture_terminal_figure.py)
runs a command on a pty and renders the resulting screen, so nothing else on
the desktop gets in the shot. `--dump` prints the screen as text, which is how
you find the right moment to stop before spending a render on it.

```bash
uv run --with pyte --with pillow python scripts/capture_terminal_figure.py \
    -o docs/guide/img/fig-1-1-doctor.png --cols 118 --rows 48 --settle 8 \
    --sub '/Users/YOURNAME=/Users/commodore' \
    -- .venv/bin/python -m c64cast --doctor --skip-probe
```

`--skip-probe` matters: a full `--doctor` adds a CONNECTIVITY section carrying
the machine's IP and hardware identity key, neither of which belongs in a
published figure. `--sub` swaps the home directory for a neutral one for the
same reason.

The wizard is interactive, so it is driven by timed keystrokes — `\r` is
RETURN, `\x1b[B` is DOWN, and the number is seconds from launch:

```bash
uv run --with pyte --with pillow python scripts/capture_terminal_figure.py \
    -o docs/guide/img/fig-2-2-wizard.png --cols 118 --rows 44 --settle 30 \
    --key '3.0:\x1b[B' --key '3.6:\r' --key '5.0:\r' --key '6.5:\r' --key '8.0:\r' \
    --key '9.5:\r' --key '11.0:\r' --key '12.5:\r' --key '14.0:\r' --key '15.5:\r' \
    --key '17.0:\x1b[B\x1b[B\x1b[B\x1b[B\x1b[B\x1b[B\x1b[B\x1b[B' \
    --key '18.0:\r' --key '19.5:\r' --key '21.0:\r' --key '22.5:\r' --key '24.0:\r' \
    -- .venv/bin/python -m c64cast --init /tmp/wizard-figure.toml
```

That sequence builds a webcam scene and a generative scene and stops on the
playlist menu, which is the state the caption describes. One ENTER too many and
it walks into the scene-type picker instead.
