---
name: hw-visual-verify
description: Visually verify a rendered change on real C64 hardware (U64/U2+/TeensyROM) by capturing the HDMI output through a USB capture device. Use when confirming overlays, display modes, palette/dither/color changes, or scene transitions actually render correctly — RAM dumps alone cannot prove what the VIC drew.
---

# Visual verification on real hardware

The U64's HTTP API lets you confirm *what was written* to screen / color RAM / VIC
registers (`/v1/machine:readmem`), but it can't tell you *what the VIC actually
rendered* — character-ROM mismatches, MCM bit-3 surprises, and mode-switch
artifacts only show up on the screen itself. When you need that ground truth and
a USB video capture device is wired to the U64's HDMI output (e.g. Elgato Cam
Link, AverMedia, any UVC capture stick), `cv2.VideoCapture(index)` will return
a 1080p BGR frame you can `imwrite()` and Read.

Don't write a capture script from scratch unless you need to. Use an existing
script in scripts/diags if one is available.

**Ask the user before assuming a capture is available** — they vary by machine. If
one is present, use it for verification of any visual change (overlays, display
modes, scene transitions) instead of guessing from RAM dumps alone.

## Finding the capture device

`c64cast --list-devices` shows each camera's name + USB VID:PID + correct index
when the `camera` extra (cv2-enumerate-cameras) is installed — so the Cam Link is
identifiable by its Elgato VID rather than by trial-and-error index probing.

`[video].device` also accepts a name substring or `VID:PID` string (resolved via
[camera.py](../../../c64cast/camera.py) `resolve_camera_index`), so a webcam scene
can target the capture stick stably.

## Scope

Local-only machine specifics (which OpenCV index is the capture device on this
host, what else is on the LAN) belong in `.claude/settings.local.json` or
auto-memory, **not** in a checked-in file.
